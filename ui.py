"""
ui.py - shared look and rendering helpers for the Live Run page and the Dashboard.

Everything a non-technical buyer sees is built here: stage cards, the decision
banner, the extracted-data tables, the PO balance panel and the check trail.
Colours use translucent backgrounds so the app works in light AND dark mode.
"""
import html
import os
import tempfile
from pathlib import Path

import pandas as pd
import pymupdf
import streamlit as st

from engine.config import DB_PATH
from engine.pipeline import STAGE_LABELS
from engine.schemas import CheckResult, CheckStatus as S, Decision, Invoice, Severity as V, StageStatus as St

GREEN, AMBER, RED, BLUE, GREY = "#16a34a", "#d97706", "#dc2626", "#2563eb", "#9ca3af"

OUTCOME_STYLE = {  # outcome -> (colour, icon, headline)
    "APPROVED": (GREEN, "✓", "Approved for payment"),
    "FLAGGED": (AMBER, "⚑", "Flagged for human review"),
    "REJECTED": (RED, "✕", "Rejected"),
}

CSS = f"""
<style>
.stage {{display:flex; gap:12px; align-items:flex-start; padding:9px 14px; margin:5px 0; border-radius:10px;
        border:1px solid rgba(128,128,128,.22); border-left:5px solid {GREY}; background:rgba(128,128,128,.05);}}
.stage .ic {{flex:0 0 26px; height:26px; border-radius:50%; display:flex; align-items:center; justify-content:center;
            font-weight:700; font-size:14px; color:#fff; background:{GREY};}}
.stage .tt {{font-weight:600; font-size:14.5px; line-height:1.3;}}
.stage .dd {{font-size:13px; opacity:.85; margin-top:2px; line-height:1.4;}}
.stage .ms {{margin-left:auto; font-size:11.5px; opacity:.55; white-space:nowrap; padding-left:8px;}}
.stage.pending {{opacity:.5;}}
.stage.running {{border-left-color:{BLUE}; background:rgba(37,99,235,.10); animation:pulse 1.1s ease-in-out infinite;}}
.stage.running .ic {{background:{BLUE};}}
.stage.done {{border-left-color:{GREEN};}}  .stage.done .ic {{background:{GREEN};}}
.stage.skip {{border-left-color:{GREY};}}   .stage.skip .ic {{background:{GREY};}}
.stage.warn {{border-left-color:{AMBER}; background:rgba(217,119,6,.07);}} .stage.warn .ic {{background:{AMBER};}}
.stage.flag {{border-left-color:{AMBER}; background:rgba(217,119,6,.12);}} .stage.flag .ic {{background:{AMBER};}}
.stage.reject {{border-left-color:{RED}; background:rgba(220,38,38,.10);}} .stage.reject .ic {{background:{RED};}}
@keyframes pulse {{0%,100%{{box-shadow:0 0 0 0 rgba(37,99,235,.35);}} 50%{{box-shadow:0 0 0 6px rgba(37,99,235,0);}}}}
.banner {{border-radius:14px; padding:16px 20px; margin:2px 0 10px 0; border:2px solid; display:flex; gap:16px; align-items:center;}}
.banner .big {{font-size:34px; font-weight:800; line-height:1;}}
.banner .hd {{font-size:20px; font-weight:700;}}
.banner .sub {{font-size:13.5px; opacity:.9; margin-top:4px; line-height:1.45;}}
[data-testid="stMetricValue"] {{font-size:1.55rem !important;}}
[data-testid="stMetricValue"] > div {{overflow:visible !important; text-overflow:clip !important; white-space:nowrap;}}
.chip {{display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:700; color:#fff;}}
</style>
"""


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


def get_db_path() -> Path:
    """Which SQLite file this browser session should use.

    Locally: the one shared data/ap.db (history survives page refreshes).
    Deployed (secret PER_SESSION_DB=1): every visitor gets a PRIVATE copy of the seeded
    mock ERP, so two evaluators never see each other's runs or trigger each other's
    duplicate rejections. Streamlit pages in one session share st.session_state.
    """
    from data.seed_erp import init_db
    if os.getenv("PER_SESSION_DB") == "1":
        path = st.session_state.get("db_path")
        if not path or not Path(path).exists():
            fd, path = tempfile.mkstemp(prefix="ap_", suffix=".db")
            os.close(fd)
            init_db(Path(path))
            st.session_state["db_path"] = path
        return Path(path)
    if not DB_PATH.exists():
        init_db()
    return DB_PATH


def money(v) -> str:
    return "-" if v in (None, "") else f"${float(v):,.2f}"


# ---------------------------------------------------------------- stage cards
def event_state(status, check: CheckResult = None) -> str:
    if status == St.RUNNING:
        return "running"
    if check is not None and check.status == S.SKIP:
        return "skip"
    if status == St.WARN:
        return "warn"
    if status == St.FAILED:
        return "reject" if (check is not None and check.severity == V.REJECT) else "flag"
    return "done"


ICONS = {"pending": "", "running": "…", "done": "✓", "skip": "–", "warn": "!", "flag": "⚑", "reject": "✕"}


def stage_card_html(label: str, state: str, detail: str = "", ms=None) -> str:
    time_txt = f"{ms} ms" if ms is not None and state not in ("pending", "running") else ""
    return (f'<div class="stage {state}"><div class="ic">{ICONS[state]}</div>'
            f'<div><div class="tt">{html.escape(label)}</div>'
            f'<div class="dd">{html.escape(detail)}</div></div><div class="ms">{time_txt}</div></div>')


def banner_html(decision: Decision, extra: str = "") -> str:
    color, icon, head = OUTCOME_STYLE[decision.outcome.value]
    po = f" &nbsp;|&nbsp; PO {decision.matched_po}" if decision.matched_po else ""
    return (f'<div class="banner" style="border-color:{color}; background:{color}1f;">'
            f'<div class="big" style="color:{color}">{icon}</div><div>'
            f'<div class="hd" style="color:{color}">{head}{po}</div>'
            f'<div class="sub">{html.escape(decision.rationale)}</div>{extra}</div></div>')


def outcome_chip(outcome: str) -> str:
    return f'<span class="chip" style="background:{OUTCOME_STYLE[outcome][0]}">{outcome}</span>'


def pdf_png(path, dpi: int = 110) -> bytes:
    doc = pymupdf.open(str(path))
    return doc[0].get_pixmap(dpi=dpi).tobytes("png")


# ---------------------------------------------------------------- decision details
FIELD_LABELS = [("vendor_name", "Vendor"), ("invoice_number", "Invoice number"), ("invoice_date", "Invoice date"),
                ("po_reference", "PO reference"), ("currency", "Currency"), ("subtotal", "Subtotal"), ("tax", "Tax"),
                ("total", "Total"), ("bank_account", "Bank account")]


def _field_value(name: str, f) -> str:
    if f.value in (None, ""):
        return "(not found)"
    if name in ("subtotal", "tax", "total"):
        return money(f.value)
    if name == "bank_account":  # never show full account numbers on screen
        digits = "".join(ch for ch in str(f.value) if ch.isdigit())
        return "••••" + digits[-4:]
    return str(f.value)


def _conf_text(f) -> str:
    if f.value in (None, ""):
        return "⚪ not found"
    dot = "🟢" if f.confidence >= 0.9 else "🟡" if f.confidence >= 0.8 else "🔴"
    return f"{dot} {f.confidence:.0%}"


def extracted_table(inv: Invoice):
    rows = [{"Field": label, "Value": _field_value(n, getattr(inv, n)), "Confidence": _conf_text(getattr(inv, n))}
            for n, label in FIELD_LABELS]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=(len(rows) + 1) * 35 + 3)


def line_items_table(inv: Invoice, decision: Decision):
    price_ev = next((c.evidence.get("lines", []) for c in decision.checks if c.name == "line_prices"), [])
    by_item = {r["item"]: r for r in price_ev}
    rows = []
    for li in inv.line_items:
        ev = by_item.get(li.description, {})
        rows.append({"Item": li.description, "Qty": li.quantity, "Invoice price": li.unit_price,
                     "PO price": ev.get("po_price"), "Variance %": ev.get("variance_pct"), "Line total": li.amount})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Invoice price": st.column_config.NumberColumn(format="$%.2f"), "PO price": st.column_config.NumberColumn(format="$%.2f"),
        "Variance %": st.column_config.NumberColumn(format="%+.2f%%"), "Line total": st.column_config.NumberColumn(format="$%.2f")})


def po_balance_panel(decision: Decision, inv: Invoice | None = None):
    chk = next((c for c in decision.checks if c.name == "po_balance" and "po_total" in c.evidence), None)
    if not chk:
        return
    ev = chk.evidence
    after = ev["billed_before"] + ev["this_invoice"]
    st.markdown(f"**Purchase order {ev['po']}: how much has been billed** (amounts before tax)")
    a, b, c_, d = st.columns(4)
    a.metric("PO value", money(ev["po_total"]))
    b.metric("Billed before", money(ev["billed_before"]))
    c_.metric("This invoice", money(ev["this_invoice"]))
    d.metric("Remaining before", money(ev["remaining_before"]))
    if ev["po_total"]:
        st.progress(min(1.0, after / ev["po_total"]), text=f"{after / ev['po_total']:.1%} of the PO would be billed after this invoice")
    tax_ev = next((c.evidence for c in decision.checks if c.name == "tax" and "stated" in c.evidence), None)
    if inv is not None and tax_ev is not None and inv.total.value not in (None, ""):
        st.caption(f"Tax is checked separately: the invoice charges {money(tax_ev['stated'])} tax and the PO's tax rate implies "
                   f"{money(tax_ev['expected'])}. Amount payable including tax: {money(inv.total.value)}.")
    if chk.status == S.WARN:  # slightly over but inside tolerance: explain instead of leaving a puzzling >100%
        st.caption(chk.detail)


def check_trail(decision: Decision):
    """The full reasoning, check by check: what the buyer needs to trust the decision."""
    for c in decision.checks:
        state = "skip" if c.status == S.SKIP else "done" if c.status == S.PASS else "warn" if c.status == S.WARN \
            else ("reject" if c.severity == V.REJECT else "flag")
        st.markdown(stage_card_html(STAGE_LABELS.get(c.name, c.name), state, c.detail), unsafe_allow_html=True)
        if c.status in (S.FAIL, S.WARN) and c.evidence:
            with st.expander(f"Evidence: {c.name}"):
                st.json(c.evidence)


def render_details(decision: Decision, inv: Invoice | None):
    """Everything under the stage list: why, what was read, how it compares, and the full trail."""
    if decision.review_reasons:
        st.markdown("#### Why a person needs to look at this")
        for r in decision.review_reasons:
            st.markdown(f"- {r}")
        if decision.suggested_action:
            st.info(f"**Suggested next step:** {decision.suggested_action}")
    if inv is not None:
        left, right = st.columns([2, 3], gap="large")
        with left:
            st.markdown("#### What the system read")
            extracted_table(inv)
        with right:
            st.markdown("#### Invoice lines vs purchase order")
            line_items_table(inv, decision)
            po_balance_panel(decision, inv)
    with st.expander("Full check-by-check reasoning trail", expanded=False):
        check_trail(decision)