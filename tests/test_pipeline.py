"""Runs all 12 sample invoices (offline fixtures, fresh temp DB, in run_order) and checks outcomes AND reasons."""
import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from data.seed_erp import init_db  # noqa: E402
from engine import pipeline  # noqa: E402

ROWS = sorted(csv.DictReader(open(ROOT / "data" / "expected.csv")), key=lambda r: int(r["run_order"]))

# The check that must be the reason, for every non-approved invoice.
EXPECTED_REASON = {
    "missing_fields_01.pdf": "completeness", "split_C.pdf": "po_balance", "bank_change_01.pdf": "bank_account",
    "no_po_01.pdf": "po_match", "dup_resubmit_01.pdf": "duplicate", "unapproved_vendor_01.pdf": "vendor",
    "exact_dup_01.pdf": "duplicate",
}


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "test.db"
    init_db(db)
    return {r["file"]: list(pipeline.run(ROOT / "data" / "invoices" / r["file"], offline=True, db_path=db))[-1].decision for r in ROWS}


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r["file"])
def test_outcome(results, row):
    assert results[row["file"]].outcome.value == row["expected_outcome"], results[row["file"]].rationale


@pytest.mark.parametrize("file,check", EXPECTED_REASON.items())
def test_reason(results, file, check):
    failing = {c.name for c in results[file].checks if c.status.value == "FAIL"}
    assert check in failing, f"{file}: expected {check} to fail, got {failing}"


def _run(db, name):
    return list(pipeline.run(ROOT / "data" / "invoices" / name, offline=True, db_path=db))[-1].decision


def test_exact_duplicate_is_rejected(tmp_path):
    db = tmp_path / "d.db"
    init_db(db)
    assert _run(db, "clean_01.pdf").outcome.value == "APPROVED"
    second = _run(db, "clean_01.pdf")  # same invoice again
    assert second.outcome.value == "REJECTED"
    assert "duplicate" in {c.name for c in second.checks if c.status.value == "FAIL"}


def test_blocked_vendor_is_rejected(tmp_path):
    import sqlite3
    db = tmp_path / "b.db"
    init_db(db)
    con = sqlite3.connect(db)
    con.execute("UPDATE vendors SET approved=0 WHERE vendor_id='V001'")
    con.commit()
    con.close()
    d = _run(db, "clean_01.pdf")
    assert d.outcome.value == "REJECTED"
    assert "vendor" in {c.name for c in d.checks if c.status.value == "FAIL"}


def test_unknown_vendor_is_held_not_rejected(tmp_path):
    import sqlite3
    db = tmp_path / "u.db"
    init_db(db)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM vendors WHERE name='Shadow Trading LLC'")  # now the supplier is simply unknown
    con.commit()
    con.close()
    d = _run(db, "unapproved_vendor_01.pdf")
    assert d.outcome.value == "FLAGGED" and "onboard" in d.suggested_action.lower()


def test_blocked_vendor_sample_is_rejected(results):
    assert results["unapproved_vendor_01.pdf"].outcome.value == "REJECTED"


def test_exact_resend_sample_is_rejected(results):
    assert results["exact_dup_01.pdf"].outcome.value == "REJECTED"


def test_no_po_suggests_right_po(results):
    assert results["no_po_01.pdf"].matched_po == "PO-1005"


def test_flagged_always_explains(results):
    for d in results.values():
        if d.outcome.value == "FLAGGED":
            assert d.review_reasons and d.suggested_action