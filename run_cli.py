"""
run_cli.py - run the pipeline from the terminal (no UI needed).

  python run_cli.py data/invoices/clean_01.pdf            one invoice (live Gemini, cached)
  python run_cli.py --all --offline                       fresh DB, all 12 invoices in order,
                                                          compared with data/expected.csv
Options: --offline use pre-computed extractions (no API calls)
"""
import argparse
import csv
import sys
from pathlib import Path

from data.seed_erp import init_db
from engine import pipeline
from engine.schemas import StageStatus as St

ROOT = Path(__file__).parent
MARK = {St.DONE: "ok  ", St.WARN: "warn", St.FAILED: "FAIL"}


def run_one(path, offline, verbose=True):
    final = None
    for ev in pipeline.run(path, offline=offline):
        if ev.status == St.RUNNING:
            continue
        if verbose:
            print(f"  [{MARK[ev.status]}] {pipeline.STAGE_LABELS[ev.stage]:34s} {ev.detail[:150]}")
        final = ev.decision or final
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    if a.all:
        init_db()
        rows = sorted(csv.DictReader(open(ROOT / "data" / "expected.csv")), key=lambda r: int(r["run_order"]))
        good = 0
        for r in rows:
            print(f"\n== {r['file']}  (expect {r['expected_outcome']}: {r['key_check']})")
            d = run_one(ROOT / "data" / "invoices" / r["file"], a.offline)
            ok = d.outcome.value == r["expected_outcome"]
            good += ok
            print(f"  => {d.outcome.value} {'OK' if ok else 'MISMATCH'}\n  {d.rationale}")
        print(f"\n{good}/{len(rows)} outcomes match expected.csv")
        sys.exit(0 if good == len(rows) else 1)
    if not a.pdf:
        ap.error("give a PDF path or --all")
    d = run_one(a.pdf, a.offline)
    print(f"\n=> {d.outcome.value}\n{d.rationale}")
    if d.suggested_action:
        print(f"Suggested action: {d.suggested_action}")


if __name__ == "__main__":
    main()
