"""
pipeline.py - runs one invoice through every stage and STREAMS what happens.

`run()` is a generator: it yields a PipelineEvent when each stage starts and when
it finishes. The Streamlit Live Run View simply renders events as they arrive,
so the screen shows the real execution, not a replay. The very last event
carries the final Decision.

    for ev in pipeline.run("data/invoices/clean_01.pdf"):
        print(ev.stage, ev.status, ev.detail)
"""
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator

from . import checks as C
from .config import connect, load_policy
from .decide import decide
from .extract import extract_invoice, file_info
from .schemas import CheckResult, CheckStatus as S, Decision, Outcome, PipelineEvent, Severity as V, StageStatus as St

# Display names for the UI, in execution order.
STAGE_LABELS = {
    "ingest": "1. Ingest document",
    "extract": "2. Extract data (LLM)",
    "completeness": "3. Completeness & confidence",
    "math_consistency": "4. Invoice arithmetic",
    "vendor": "5. Vendor verification",
    "duplicate": "6. Duplicate detection",
    "po_match": "7. Purchase order match",
    "line_prices": "8. Price check vs PO",
    "po_balance": "9. PO balance (split billing)",
    "tax": "10. Tax check",
    "bank_account": "11. Bank account audit",
    "decision": "12. Decision & explanation",
}

_STATUS = {S.PASS: St.DONE, S.SKIP: St.DONE, S.WARN: St.WARN, S.FAIL: St.FAILED}


def _persist(con, run_id, filename, started, inv, decision, ctx_vendor):
    """Save the run (Dashboard) and the invoice (history for duplicates and PO balances)."""
    now = datetime.now().isoformat(timespec="seconds")
    con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, filename, started, now, decision.outcome.value, decision.rationale, decision.matched_po,
                 inv.model_dump_json() if inv else None, decision.model_dump_json()))
    if inv is not None:
        lines = [li.model_dump() for li in inv.line_items]
        con.execute("INSERT INTO invoices (vendor_id,invoice_number,invoice_date,subtotal,total,po_number,status,source_file,lines_json,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ctx_vendor["vendor_id"] if ctx_vendor else None, inv.invoice_number.value, inv.invoice_date.value,
                     C.num(inv.subtotal), C.num(inv.total), decision.matched_po, decision.outcome.value, filename, json.dumps(lines), now))
    con.commit()


def run(pdf_path, *, offline: bool = False, db_path=None, persist: bool = True, step_delay: float = 0.0) -> Iterator[PipelineEvent]:
    """step_delay: seconds to pause after each stage so a live demo is watchable (0 for tests)."""
    pdf_path = Path(pdf_path)
    con, pol = connect(db_path), load_policy()
    run_id, started = uuid.uuid4().hex[:8], datetime.now().isoformat(timespec="seconds")

    def done(stage, status, detail, t0, **kw):
        if step_delay:
            time.sleep(step_delay)
        return PipelineEvent(stage=stage, status=status, detail=detail, duration_ms=int((time.perf_counter() - t0) * 1000), **kw)

    # ---- stage 1: ingest -------------------------------------------------------
    t0 = time.perf_counter()
    yield PipelineEvent(stage="ingest", status=St.RUNNING, detail=f"Reading {pdf_path.name}")
    info = file_info(pdf_path)
    kind = "text PDF" if info["kind"] == "text" else "scanned image (no text layer, so vision extraction is needed)"
    yield done("ingest", St.DONE, f"{info['pages']} page(s), {kind}. Fingerprint {info['sha256'][:10]}", t0, data=info)

    # ---- stage 2: extract ------------------------------------------------------
    t0 = time.perf_counter()
    yield PipelineEvent(stage="extract", status=St.RUNNING, detail="Reading fields and line items")
    try:
        inv, meta = extract_invoice(pdf_path, offline=offline)
    except Exception as e:  # noqa: BLE001  extraction failure is a business outcome, not a crash
        fail = CheckResult(name="extraction", status=S.FAIL, severity=V.FLAG, detail=f"The document could not be read automatically ({type(e).__name__}: {e}).")
        d = decide([fail])
        yield done("extract", St.FAILED, fail.detail, t0, check=fail)
        if persist:
            _persist(con, run_id, pdf_path.name, started, None, d, None)
        yield done("decision", St.DONE, d.outcome.value, t0, decision=d)
        return
    yield done("extract", St.DONE, f"Extracted via {meta['source']} ({meta['note']}). {len(inv.line_items)} line item(s).", t0,
               data={"invoice": inv.model_dump(), "meta": meta})

    # ---- stages 3-11: the deterministic checks ---------------------------------
    ctx = C.Ctx(inv=inv, pol=pol, db=con)
    results = []
    for name, fn in C.CHECKS:
        t0 = time.perf_counter()
        yield PipelineEvent(stage=name, status=St.RUNNING)
        r = fn(ctx)
        results.append(r)
        yield done(name, _STATUS[r.status], r.detail, t0, check=r)

    # ---- stage 12: decide ------------------------------------------------------
    t0 = time.perf_counter()
    yield PipelineEvent(stage="decision", status=St.RUNNING, detail="Applying decision rules")
    decision = decide(results, matched_po=ctx.po["po_number"] if ctx.po else None)
    if persist:
        _persist(con, run_id, pdf_path.name, started, inv, decision, ctx.vendor)
    yield done("decision", St.DONE, f"{decision.outcome.value}", t0, decision=decision, data={"run_id": run_id})
