"""
extract.py - turns an invoice PDF into an `Invoice` (see schemas.py).

The LLM ONLY reads the document. It never judges it.

Source order for every extraction (this is the demo fail-safe):
    1. CACHE    .cache/extractions/<sha256>.json   (same file -> same answer, instant, free)
    2. GEMINI   native PDF input, so text PDFs AND scanned images both work
    3. FIXTURE  data/fixtures/<filename>.json      (pre-computed; used with offline=True,
                or automatically if Gemini is unreachable / rate-limited)

Quick live test of your API key:
    python -m engine.extract data/invoices/clean_01.pdf
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import pymupdf
from pydantic import BaseModel

from .config import ROOT, load_policy
from .schemas import ExtractedField, Invoice, LineItem

CACHE_DIR = ROOT / ".cache" / "extractions"
FIXTURE_DIR = ROOT / "data" / "fixtures"

PROMPT = """You are a precise data-extraction engine for accounts-payable invoices.
Read the attached invoice and return the requested JSON. Rules:
- Copy values exactly as printed. NEVER guess, infer or invent. If a field is not on the document, return null for its value and 0 for confidence.
- confidence: 0.0-1.0, how sure you are that the value is read correctly (lower for blurry, ambiguous or partly legible text).
- source_text: the short snippet of the document the value came from.
- Dates in ISO format YYYY-MM-DD. Money as plain numbers as strings, no currency symbols or thousands separators (e.g. "1234.50").
- po_reference: the purchase order number if the invoice mentions one under ANY label (PO Number, Your Ref, Purchase Order, ...). Otherwise null.
- bank_account: the account number the vendor asks to be paid into. Otherwise null.
- tax: the tax amount in money, or null if no tax line exists.
- line_items: every row of the item table, in order. quantity/unit_price/amount are numbers or null.
"""


# ---- schema shown to the LLM (flat, everything nullable, no defaults: Gemini-friendly) ----
class LLMField(BaseModel):
    value: Optional[str]
    confidence: float
    source_text: Optional[str]


class LLMLine(BaseModel):
    description: str
    quantity: Optional[float]
    unit_price: Optional[float]
    amount: Optional[float]


class LLMInvoice(BaseModel):
    vendor_name: LLMField
    invoice_number: LLMField
    invoice_date: LLMField
    po_reference: LLMField
    currency: LLMField
    subtotal: LLMField
    tax: LLMField
    total: LLMField
    bank_account: LLMField
    line_items: list[LLMLine]


MONEY_FIELDS = ("subtotal", "tax", "total")


def parse_money(text) -> Optional[float]:
    """'$1,234.50' -> 1234.5 ; garbage -> None (never crash on a bad read)."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = re.sub(r"[^0-9.\-]", "", str(text))
    try:
        return float(cleaned)
    except ValueError:
        return None


def to_invoice(llm: LLMInvoice) -> Invoice:
    """LLM output (strings) -> our typed Invoice (numbers parsed in code, not by the LLM)."""
    fields = {}
    for name in Invoice.model_fields:
        if name == "line_items":
            continue
        f = getattr(llm, name)
        value = parse_money(f.value) if name in MONEY_FIELDS else f.value
        value = None if value in ("", None) else value
        fields[name] = ExtractedField(value=value, confidence=0.0 if value is None else f.confidence, source_text=f.source_text)
    lines = [LineItem(**li.model_dump()) for li in llm.line_items]
    return Invoice(**fields, line_items=lines)


# ---------------------------------------------------------------- ingest helpers
def file_info(pdf_path) -> dict:
    """Stage 1 facts: content hash, page count, and whether the PDF has a text layer."""
    data = Path(pdf_path).read_bytes()
    doc = pymupdf.open(stream=data, filetype="pdf")
    chars = sum(len(p.get_text().strip()) for p in doc)
    return {"sha256": hashlib.sha256(data).hexdigest(), "pages": len(doc), "text_chars": chars,
            "kind": "text" if chars > 50 else "scanned"}


# ---------------------------------------------------------------- Gemini call
def _call_gemini(pdf_bytes: bytes) -> LLMInvoice:
    from dotenv import load_dotenv
    from google import genai
    from google.genai import types

    load_dotenv(ROOT / ".env")
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not found (put it in the .env file in the project root)")
    pol = load_policy()["llm"]
    client = genai.Client(api_key=key)
    opts = dict(system_instruction=PROMPT, response_mime_type="application/json", response_schema=LLMInvoice)
    if pol.get("temperature") is not None:  # newer Gemini models may reject sampling parameters, so only send it if set
        opts["temperature"] = pol["temperature"]
    config = types.GenerateContentConfig(**opts)
    models = pol.get("models") or [pol["model"]]  # free-tier quotas are per model, so a second model is a real fallback
    last = None
    for model in models:
        for attempt in range(2):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), "Extract this invoice."],
                    config=config)
                return (resp.parsed or LLMInvoice.model_validate_json(resp.text)), model
            except Exception as e:  # noqa: BLE001
                last = e
                if any(t in str(e) for t in ("429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE")) and attempt == 0:
                    time.sleep(4)  # brief wait in case it is only the per-minute limit
                    continue
                break  # quota gone, bad model name, etc.: move on to the next model
    raise last


def extract_invoice(pdf_path, offline: bool = False) -> tuple[Invoice, dict]:
    """Returns (Invoice, meta). meta['source'] is 'cache' | 'gemini' | 'fixture'."""
    pdf_path = Path(pdf_path)
    info = file_info(pdf_path)
    cache_file = CACHE_DIR / f"{info['sha256']}.json"
    fixture_file = FIXTURE_DIR / f"{pdf_path.name}.json"

    if offline:
        return Invoice.model_validate_json(fixture_file.read_text()), {"source": "fixture", "note": "offline mode"}
    if cache_file.exists():
        return Invoice.model_validate_json(cache_file.read_text()), {"source": "cache", "note": "same file seen before"}
    try:
        llm, used_model = _call_gemini(pdf_path.read_bytes())
        inv = to_invoice(llm)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(inv.model_dump_json(indent=2))
        return inv, {"source": "gemini", "note": used_model}
    except Exception as e:  # noqa: BLE001
        if fixture_file.exists():  # demo safety net, always disclosed in the run trace
            why = " ".join(str(e).split())[:230]  # the real API message, so the cause (quota? model? key?) is visible
            return (Invoice.model_validate_json(fixture_file.read_text()),
                    {"source": "fixture", "note": f"Gemini unavailable ({type(e).__name__}: {why}); used pre-computed extraction"})
        raise


if __name__ == "__main__":
    import sys
    invoice, meta = extract_invoice(sys.argv[1])
    print(meta)
    print(invoice.model_dump_json(indent=2))