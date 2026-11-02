"""
seed_erp.py - builds the mock procurement system ("the ERP") in SQLite.

Run from the project root:   python data/seed_erp.py
It RESETS data/ap.db every time, so demos and tests always start clean.

Tables
  vendors          approved-vendor master + the bank account we have on file
  purchase_orders  one row per PO (tax rate lives here)
  po_lines         the items/qty/price each PO authorises
  invoices         processed-invoice history (duplicate detection + PO balance)
  runs             one row per pipeline run (feeds the Dashboard)

Every PO/vendor below exists to power one specific sample invoice. The mapping
is documented in data/expected.csv (written by generate_invoices.py).
"""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "ap.db"

SCHEMA = """
CREATE TABLE vendors (
    vendor_id TEXT PRIMARY KEY, name TEXT, approved INTEGER,
    bank_account TEXT, bank_name TEXT, contact_phone TEXT);
CREATE TABLE purchase_orders (
    po_number TEXT PRIMARY KEY, vendor_id TEXT, issued_date TEXT,
    tax_rate REAL, currency TEXT, status TEXT);
CREATE TABLE po_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, line_no INTEGER,
    description TEXT, quantity REAL, unit_price REAL);
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id TEXT, invoice_number TEXT,
    invoice_date TEXT, subtotal REAL, total REAL, po_number TEXT,
    status TEXT, source_file TEXT, lines_json TEXT, created_at TEXT);
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, filename TEXT, started_at TEXT, finished_at TEXT,
    outcome TEXT, rationale TEXT, matched_po TEXT,
    extraction_json TEXT, decision_json TEXT);
"""

# (vendor_id, name, approved, bank_account, bank_name, phone)
VENDORS = [
    ("V001", "Northwind Stationers",   1, "1100-2200-3301", "First Meridian Bank", "+1-555-0101"),
    ("V002", "BrightLine Logistics",   1, "1100-2200-3302", "First Meridian Bank", "+1-555-0102"),
    ("V003", "Apex Industrial Supply", 1, "1100-2200-3303", "Lakeshore Bank",      "+1-555-0103"),
    ("V004", "Meridian IT Services",   1, "1100-2200-3304", "Lakeshore Bank",      "+1-555-0104"),
    ("V005", "Greenfield Catering Co", 1, "1100-2200-3305", "Harbor Trust",        "+1-555-0105"),
]

# po_number: (vendor, issued, tax_rate, [(description, qty, unit_price), ...])
POS = {
    "PO-1001": ("V001", "2026-09-20", 0.08, [("A4 Copier Paper (ream)", 200, 4.50), ("Toner Cartridge HP 26A", 20, 60.00)]),
    "PO-1002": ("V002", "2026-09-22", 0.00, [("Freight - Zone A shipment", 10, 250.00), ("Warehouse handling (per hour)", 40, 25.00)]),
    "PO-1003": ("V003", "2026-09-25", 0.08, [("Safety gloves (box of 12)", 50, 18.00), ("Hard hat, ANSI rated", 30, 22.00)]),
    # PO-1004 is the split-invoicing PO: 100 hours in total, billed in three stages
    "PO-1004": ("V004", "2026-08-30", 0.00, [("Software development hours", 100, 100.00)]),
    "PO-1005": ("V005", "2026-10-01", 0.05, [("Lunch boxes (standard)", 300, 12.00), ("Coffee service", 10, 80.00)]),
    "PO-1006": ("V001", "2026-10-02", 0.08, [("Printer maintenance (monthly)", 12, 150.00)]),
    "PO-1007": ("V005", "2026-10-03", 0.05, [("Event decor rental", 1, 2500.00)]),  # fuzzy-match distractor
    "PO-1008": ("V003", "2026-10-05", 0.08, [("Safety signage panels", 40, 35.00), ("Floor marking tape rolls", 20, 80.00)]),
    "PO-1009": ("V004", "2026-10-06", 0.00, [("Cloud hosting - monthly", 12, 400.00)]),
    "PO-1010": ("V002", "2026-10-07", 0.00, [("Pallet wrap roll", 100, 8.00), ("Strapping reel", 50, 6.00)]),
}

# Already-processed invoice: the original that "dup_resubmit_01" will echo.
HISTORY = [{
    "vendor_id": "V003", "invoice_number": "INV-2041", "invoice_date": "2026-10-14",
    "subtotal": 1400.00, "total": 1512.00, "po_number": "PO-1008", "status": "APPROVED",
    "source_file": "history_seed",
    "lines": [{"description": "Safety signage panels", "quantity": 40, "unit_price": 35.00, "amount": 1400.00}],
}]


def init_db(path: Path = DB_PATH) -> None:
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO vendors VALUES (?,?,?,?,?,?)", VENDORS)
    for po, (vendor, issued, tax, lines) in POS.items():
        con.execute("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?)", (po, vendor, issued, tax, "USD", "OPEN"))
        for i, (desc, qty, price) in enumerate(lines, 1):
            con.execute("INSERT INTO po_lines (po_number,line_no,description,quantity,unit_price) VALUES (?,?,?,?,?)",
                        (po, i, desc, qty, price))
    for h in HISTORY:
        con.execute(
            "INSERT INTO invoices (vendor_id,invoice_number,invoice_date,subtotal,total,po_number,status,source_file,lines_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (h["vendor_id"], h["invoice_number"], h["invoice_date"], h["subtotal"], h["total"], h["po_number"],
             h["status"], h["source_file"], json.dumps(h["lines"]), h["invoice_date"]))
    con.commit()
    con.close()


if __name__ == "__main__":
    init_db()
    con = sqlite3.connect(DB_PATH)
    for t in ("vendors", "purchase_orders", "po_lines", "invoices"):
        print(f"{t:16s} {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]} rows")
    print(f"Seeded {DB_PATH}")
