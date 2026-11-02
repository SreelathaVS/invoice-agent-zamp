"""
compare_extractions.py - how accurate was the live (Gemini) extraction?

Compares what Gemini returned (.cache/extractions) with the ground truth we
generated the PDFs from (data/fixtures). Prints every field that differs.

    python compare_extractions.py

Useful twice: (1) to find real extraction problems before the demo,
(2) as a number you can quote: "I measured extraction accuracy against ground truth."
"""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PDFS, FIXTURES, CACHE = ROOT / "data" / "invoices", ROOT / "data" / "fixtures", ROOT / ".cache" / "extractions"
HEADER = ["vendor_name", "invoice_number", "invoice_date", "po_reference", "currency", "subtotal", "tax", "total", "bank_account"]


def norm(name, v):
    """Make two representations of the same value comparable."""
    if v in (None, ""):
        return None
    if name == "bank_account":
        return re.sub(r"\D", "", str(v))
    try:
        x = round(float(str(v).replace("$", "").replace(",", "")), 2)
        return None if (name == "tax" and x == 0) else x  # 'no tax line' may come back as null OR 0.00
    except ValueError:
        return " ".join(str(v).lower().split())


def compare(truth: dict, got: dict):
    total, bad = 0, []
    for f in HEADER:
        total += 1
        t, g = norm(f, truth[f]["value"]), norm(f, got[f]["value"])
        if t != g:
            bad.append(f"{f}: expected {truth[f]['value']!r}, got {got[f]['value']!r} (confidence {got[f]['confidence']:.0%})")
    tl, gl = truth["line_items"], got["line_items"]
    total += 1
    if len(tl) != len(gl):
        bad.append(f"line item count: expected {len(tl)}, got {len(gl)}")
    for i, (a, b) in enumerate(zip(tl, gl), 1):
        for k in ("description", "quantity", "unit_price", "amount"):
            total += 1
            if norm(k, a[k]) != norm(k, b[k]):
                bad.append(f"line {i} {k}: expected {a[k]!r}, got {b[k]!r}")
    return total, bad


def main():
    grand_total = grand_bad = checked = 0
    for pdf in sorted(PDFS.glob("*.pdf")):
        cache_file = CACHE / f"{hashlib.sha256(pdf.read_bytes()).hexdigest()}.json"
        fixture = FIXTURES / f"{pdf.name}.json"
        if not cache_file.exists():
            print(f"[ -- ] {pdf.name:28s} not in cache (run it once with Offline mode off)")
            continue
        total, bad = compare(json.loads(fixture.read_text()), json.loads(cache_file.read_text()))
        checked += 1
        grand_total += total
        grand_bad += len(bad)
        print(f"[{'ok' if not bad else 'DIFF':^4}] {pdf.name:28s} {total - len(bad)}/{total} values match")
        for b in bad:
            print(f"         - {b}")
    if checked:
        print(f"\n{checked} invoice(s) compared: {grand_total - grand_bad}/{grand_total} values correct ({(grand_total - grand_bad) / grand_total:.1%})")
    else:
        print("Nothing to compare: the cache is empty.")


if __name__ == "__main__":
    main()