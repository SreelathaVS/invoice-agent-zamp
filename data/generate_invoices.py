"""
generate_invoices.py - creates the 12 synthetic invoice PDFs + expected.csv.

Run from the project root:   python data/generate_invoices.py

Messiness is deliberate, because real vendors are messy:
  * 3 different layouts, with different labels for the same thing
    ("PO Number" / "Your Ref" / "Purchase Order")
  * tax itemised on some invoices, absent on others
  * one SCANNED invoice (image only, slightly rotated, noisy: no text layer)
  * fields missing on purpose (invoice number, date, PO reference)

Each invoice has a known expected outcome, written to data/expected.csv.
IMPORTANT: run them in `run_order` on a freshly seeded DB; the split-invoice
trio depends on earlier approvals consuming the PO balance.
"""
import csv
import json
import random
from pathlib import Path

import pymupdf as fitz  # PyMuPDF
import numpy as np
from PIL import Image
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "invoices"
FIXTURES = Path(__file__).parent / "fixtures"
W, H = A4

VENDORS = {
    "V001": dict(name="Northwind Stationers", addr="14 Orchard Row, Springfield, IL 62701", bank="1100-2200-3301", bank_name="First Meridian Bank"),
    "V002": dict(name="BrightLine Logistics", addr="900 Harbor Way, Newark, NJ 07102", bank="1100-2200-3302", bank_name="First Meridian Bank"),
    "V003": dict(name="Apex Industrial Supply", addr="27 Foundry Rd, Dayton, OH 45402", bank="1100-2200-3303", bank_name="Lakeshore Bank"),
    "V004": dict(name="Meridian IT Services", addr="5 Binary Park, Austin, TX 78701", bank="1100-2200-3304", bank_name="Lakeshore Bank"),
    "V005": dict(name="Greenfield Catering Co", addr="3 Maple Court, Portland, OR 97201", bank="1100-2200-3305", bank_name="Harbor Trust"),
    "XXXX": dict(name="Shadow Trading LLC", addr="Suite 9, 1 Nowhere Plaza, Miami, FL 33101", bank="9000-0000-1234", bank_name="Offshore Credit Union"),
}

# ---------------------------------------------------------------- invoice specs
# omit: header fields deliberately left off the page. lines: (desc, qty, unit_price)
SPECS = [
    dict(order=1, file="clean_01.pdf", layout="classic", vendor="V001", number="INV-NW-3301", date="2026-10-05",
         po="PO-1001", lines=[("A4 Copier Paper (ream)", 200, 4.50), ("Toner Cartridge HP 26A", 20, 60.00)], tax=0.08,
         expect="APPROVED", edge="happy path", key="all checks pass; tax itemised"),
    dict(order=2, file="clean_02.pdf", layout="minimal", vendor="V002", number="INV-BL-0877", date="2026-10-08",
         po="PO-1002", lines=[("Freight - Zone A shipment", 10, 250.00), ("Warehouse handling (per hour)", 40, 25.00)], tax=None,
         expect="APPROVED", edge="happy path", key="no tax line; different layout"),
    dict(order=3, file="clean_03.pdf", layout="modern", vendor="V003", number="INV-AIS-5521", date="2026-10-10",
         po="PO-1003", lines=[("Safety gloves (box of 12)", 50, 18.20), ("Hard hat, ANSI rated", 30, 22.00)], tax=0.08,
         expect="APPROVED", edge="happy path + tolerance", key="gloves price +1.1% -> WARN inside 2% tolerance"),
    dict(order=4, file="scanned_01.pdf", layout="classic", vendor="V002", number="INV-BL-0881", date="2026-10-12",
         po="PO-1010", lines=[("Pallet wrap roll", 100, 8.00), ("Strapping reel", 50, 6.00)], tax=None, scanned=True,
         expect="APPROVED", edge="scanned image", key="no text layer; vision extraction path"),
    dict(order=5, file="missing_fields_01.pdf", layout="minimal", vendor="V004", number="", date="", omit=["number", "date"],
         po="PO-1009", lines=[("Cloud hosting - monthly", 12, 400.00)], tax=None,
         expect="FLAGGED", edge="missing metadata", key="no invoice number or date -> human review"),
    # ---- edge case 1: split invoicing against one PO (PO-1004 = 100 hours / $10,000) ----
    dict(order=6, file="split_A.pdf", layout="modern", vendor="V004", number="INV-MIT-1101", date="2026-09-15",
         po="PO-1004", lines=[("Software development hours", 40, 100.00)], tax=None,
         expect="APPROVED", edge="1 split invoicing", key="stage 1: 40 of 100 hours; $6,000 left"),
    dict(order=7, file="split_B.pdf", layout="modern", vendor="V004", number="INV-MIT-1102", date="2026-10-05",
         po="PO-1004", lines=[("Software development hours", 35, 100.00)], tax=None,
         expect="APPROVED", edge="1 split invoicing", key="stage 2: 35 hours; $2,500 / 25 hours left"),
    dict(order=8, file="split_C.pdf", layout="modern", vendor="V004", number="INV-MIT-1103", date="2026-10-28",
         po="PO-1004", lines=[("Software development hours", 32, 100.00)], tax=None,
         expect="FLAGGED", edge="1 split invoicing", key="32 hours billed but only 25 remain: PO overrun of $700"),
    # ---- edge case 2: remittance bank account changed ----
    dict(order=9, file="bank_change_01.pdf", layout="classic", vendor="V001", number="INV-NW-3345", date="2026-10-20",
         po="PO-1006", lines=[("Printer maintenance (monthly)", 12, 150.00)], tax=0.08, bank_override="1100-2200-9999",
         expect="FLAGGED", edge="2 bank mismatch", key="everything else perfect, but account differs from vendor master"),
    # ---- edge case 3: no PO reference -> fuzzy match ----
    dict(order=10, file="no_po_01.pdf", layout="minimal", vendor="V005", number="INV-GF-0412", date="2026-10-22",
         po=None, omit=["po"], lines=[("Boxed lunches", 300, 12.00), ("Coffee station service", 10, 80.00)], tax=0.05,
         expect="FLAGGED", edge="3 missing PO + fuzzy match", key="suggest PO-1005 (clear winner vs PO-1007); never auto-approve"),
    # ---- edge case 4: near-duplicate resubmission of history invoice INV-2041 ----
    dict(order=11, file="dup_resubmit_01.pdf", layout="modern", vendor="V003", number="INV2041-R", date="2026-10-24",
         po="PO-1008", lines=[("Safety signage panels", 40, 35.00)], tax=0.08,
         expect="FLAGGED", edge="4 near-duplicate", key="same vendor/amount/lines as INV-2041, new number+date"),
    # ---- unknown supplier: hold pending onboarding ----
    dict(order=12, file="unapproved_vendor_01.pdf", layout="minimal", vendor="XXXX", number="INV-ST-77", date="2026-10-27",
         po=None, omit=["po"], lines=[("Office supplies, assorted", 1, 950.00)], tax=None,
         expect="FLAGGED", edge="unknown vendor", key="supplier not in vendor master: hold pending onboarding"),
]

LABELS = {  # same facts, different words per layout: this is what the LLM must cope with
    "classic": dict(number="Invoice No.", date="Invoice Date", po="PO Number", due="Due Date",
                    cols=("Description", "Qty", "Unit Price", "Amount"), sub="Subtotal", tax="Sales Tax", total="TOTAL DUE"),
    "minimal": dict(number="Invoice #", date="Date", po="Your Ref", due="Payable by",
                    cols=("Item", "Qty", "Rate", "Total"), sub="Net", tax="Tax", total="Amount payable"),
    "modern":  dict(number="Inv No", date="Issued", po="Purchase Order", due="Due",
                    cols=("Service / Goods", "Units", "Price", "Line total"), sub="Subtotal", tax="VAT/Tax", total="Balance Due"),
}


def money(v):
    return f"${v:,.2f}"


def totals(spec):
    sub = round(sum(q * p for _, q, p in spec["lines"]), 2)
    tax = round(sub * spec["tax"], 2) if spec.get("tax") else 0.0
    return sub, tax, round(sub + tax, 2)


def meta_rows(spec, lab):
    rows = []
    omit = spec.get("omit", [])
    if "number" not in omit: rows.append((lab["number"], spec["number"]))
    if "date" not in omit:
        rows.append((lab["date"], spec["date"]))
        y, m, d = spec["date"].split("-")
        rows.append((lab["due"], f"{y}-{int(m) % 12 + 1:02d}-{d}" if int(m) < 12 else f"{int(y)+1}-01-{d}"))
    if "po" not in omit and spec.get("po"): rows.append((lab["po"], spec["po"]))
    return rows


def draw_table(c, spec, lab, y, xs, header_fill=None, grid=False):
    c.setFont("Helvetica-Bold", 9)
    if header_fill:
        c.setFillColor(HexColor(header_fill)); c.rect(xs[0] - 6, y - 6, xs[4] - xs[0] + 12, 20, fill=1, stroke=0)
        c.setFillColor(white)
    c.drawString(xs[0], y, lab["cols"][0])
    for i in (1, 2, 3):
        c.drawRightString(xs[i + 1], y, lab["cols"][i])
    c.setFillColor(black); c.setFont("Helvetica", 9)
    y -= 22
    for desc, q, p in spec["lines"]:
        c.drawString(xs[0], y, desc)
        c.drawRightString(xs[2], y, f"{q:g}")
        c.drawRightString(xs[3], y, money(p))
        c.drawRightString(xs[4], y, money(q * p))
        if grid:
            c.setStrokeColor(HexColor("#cccccc")); c.line(xs[0] - 6, y - 7, xs[4] + 6, y - 7)
        y -= 20
    return y


def draw_totals(c, spec, lab, y, right=W - 40):
    sub, tax, total = totals(spec)
    c.setFont("Helvetica", 10)
    c.drawRightString(right - 90, y, lab["sub"]); c.drawRightString(right, y, money(sub)); y -= 16
    if spec.get("tax"):
        c.drawRightString(right - 90, y, f"{lab['tax']} ({spec['tax']*100:g}%)"); c.drawRightString(right, y, money(tax)); y -= 16
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(right - 90, y, lab["total"]); c.drawRightString(right, y, money(total)); y -= 10
    return y


def draw_bank(c, spec, v, y, x=40):
    acct = spec.get("bank_override", v["bank"])
    c.setFont("Helvetica", 8.5); c.setFillColor(HexColor("#444444"))
    c.drawString(x, y, f"Please remit payment to: {v['bank_name']}  |  Account No. {acct}  |  Currency: USD")
    c.drawString(x, y - 12, "Thank you for your business. Payment terms: net 30 days.")
    c.setFillColor(black)


def render(spec, path):
    v = VENDORS[spec["vendor"]]
    lab = LABELS[spec["layout"]]
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)  # invariant: same bytes every run -> stable cache keys
    layout = spec["layout"]
    if layout == "classic":
        c.setFillColor(HexColor("#1f3a5f")); c.rect(0, H - 90, W, 90, fill=1, stroke=0); c.setFillColor(white)
        c.setFont("Helvetica-Bold", 20); c.drawString(40, H - 50, v["name"])
        c.setFont("Helvetica", 9); c.drawString(40, H - 68, v["addr"])
        c.setFont("Helvetica-Bold", 26); c.drawRightString(W - 40, H - 55, "INVOICE"); c.setFillColor(black)
        y = H - 125
        c.setFont("Helvetica", 10)
        for k, val in meta_rows(spec, lab):
            c.setFont("Helvetica-Bold", 10); c.drawString(40, y, k + ":"); c.setFont("Helvetica", 10); c.drawString(140, y, val); y -= 16
        y -= 20
        y = draw_table(c, spec, lab, y, (50, 330, 400, 480, W - 50), header_fill="#1f3a5f", grid=True)
        y = draw_totals(c, spec, lab, y - 14)
        draw_bank(c, spec, v, 90)
    elif layout == "minimal":
        c.setFont("Helvetica-Bold", 16); c.drawString(40, H - 60, v["name"])
        c.setFont("Helvetica", 9); c.drawString(40, H - 75, v["addr"])
        c.setFont("Helvetica", 14); c.drawString(40, H - 115, "Bill")
        y = H - 140
        c.setFont("Helvetica", 10)
        for k, val in meta_rows(spec, lab):
            c.drawString(40, y, f"{k}: {val}"); y -= 15
        y -= 24
        y = draw_table(c, spec, lab, y, (40, 320, 390, 470, W - 40))
        y = draw_totals(c, spec, lab, y - 16)
        draw_bank(c, spec, v, 100)
    else:  # modern: coloured sidebar holds the metadata
        c.setFillColor(HexColor("#0f766e")); c.rect(0, 0, 150, H, fill=1, stroke=0); c.setFillColor(white)
        words = v["name"].split(); half = (len(words) + 1) // 2
        c.setFont("Helvetica-Bold", 13)
        c.drawString(16, H - 50, " ".join(words[:half])); c.drawString(16, H - 65, " ".join(words[half:]))
        c.setFont("Helvetica", 8)
        street, city = v["addr"].split(", ", 1)
        c.drawString(16, H - 82, street); c.drawString(16, H - 93, city)
        y = H - 130
        for k, val in meta_rows(spec, lab):
            c.setFont("Helvetica", 8); c.drawString(16, y, k.upper()); c.setFont("Helvetica-Bold", 10); c.drawString(16, y - 13, val); y -= 38
        c.setFillColor(black)
        c.setFont("Helvetica-Bold", 22); c.drawString(180, H - 55, "Tax Invoice" if spec.get("tax") else "Invoice")
        y = draw_table(c, spec, lab, H - 120, (180, 380, 430, 500, W - 40), header_fill="#0f766e")
        y = draw_totals(c, spec, lab, y - 14)
        draw_bank(c, spec, v, 90, x=170)  # footer sits right of the sidebar
    c.showPage(); c.save()


def make_scanned(path, seed=7):
    """Replace the text PDF with an image-only PDF: rotated, noisy, JPEG-compressed."""
    rng = np.random.default_rng(seed)
    src = fitz.open(path)
    pix = src[0].get_pixmap(dpi=130)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    img = img.rotate(0.8, fillcolor=255, resample=Image.BICUBIC)
    arr = np.asarray(img, dtype=np.float32) + rng.normal(0, 9, (img.height, img.width))
    img = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))
    tmp = Path(path).with_suffix(".jpg"); img.save(tmp, quality=55)
    out = fitz.open(); page = out.new_page(width=W, height=H); page.insert_image(page.rect, filename=str(tmp))
    src.close(); out.save(str(path) + ".tmp"); out.close(); tmp.unlink()
    Path(str(path) + ".tmp").replace(path)


def fixture(spec):
    """Pre-computed 'perfect' extraction for one invoice (see engine/extract.py).
    Used for offline tests and as the demo safety net if the Gemini API is unreachable."""
    v, omit = VENDORS[spec["vendor"]], spec.get("omit", [])
    sub, tax, total = totals(spec)
    conf = 0.90 if spec.get("scanned") else 0.97

    def f(value, src=None):
        return {"value": value, "confidence": conf if value not in (None, "") else 0.0, "source_text": src}
    return {
        "vendor_name": f(v["name"]),
        "invoice_number": f(None if "number" in omit else spec["number"]),
        "invoice_date": f(None if "date" in omit else spec["date"]),
        "po_reference": f(None if "po" in omit else spec.get("po")),
        "currency": f("USD"),
        "subtotal": f(sub), "tax": f(tax if spec.get("tax") else None), "total": f(total),
        "bank_account": f(spec.get("bank_override", v["bank"])),
        "line_items": [{"description": d, "quantity": q, "unit_price": p, "amount": round(q * p, 2)} for d, q, p in spec["lines"]],
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    random.seed(1)
    for spec in SPECS:
        p = OUT / spec["file"]
        render(spec, p)
        if spec.get("scanned"):
            make_scanned(p)
        (FIXTURES / f"{spec['file']}.json").write_text(json.dumps(fixture(spec), indent=2))
    with open(OUT.parent / "expected.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run_order", "file", "expected_outcome", "edge_case", "key_check"])
        for s in SPECS:
            w.writerow([s["order"], s["file"], s["expect"], s["edge"], s["key"]])
    print(f"Wrote {len(SPECS)} invoices to {OUT} and data/expected.csv")


if __name__ == "__main__":
    main()