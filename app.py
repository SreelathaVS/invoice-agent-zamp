"""
app.py - LIVE RUN VIEW.   Start with:   streamlit run app.py

Pick (or upload) an invoice, press Run, and watch every stage execute for real:
left = the document, right = the 12 stages lighting up as the pipeline streams
events. The decision banner appears at the top when the last stage completes.
"""
import csv
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from data.seed_erp import init_db
from engine import pipeline
from engine.extract import ROOT
from engine.schemas import Invoice
from ui import (banner_html, ensure_db, event_state, inject_css, pdf_png, render_details, stage_card_html)

st.set_page_config(page_title="AP Invoice Agent: Live Run", page_icon="🧾", layout="wide")
inject_css()
ensure_db()
load_dotenv(ROOT / ".env")

SAMPLES = ROOT / "data" / "invoices"
UPLOADS = ROOT / "data" / "uploads"
sample_rows = sorted(csv.DictReader(open(ROOT / "data" / "expected.csv")), key=lambda r: int(r["run_order"]))

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Controls")
    has_key = bool(os.getenv("GEMINI_API_KEY"))
    offline = st.toggle("Offline mode (pre-computed extraction)", value=not has_key,
                        help="Uses stored extractions for the sample invoices instead of calling Gemini. Safe fallback for the demo.")
    if not has_key and not offline:
        st.warning("No GEMINI_API_KEY found in .env")
    delay = st.slider("Demo pacing (seconds per stage)", 0.0, 1.5, 0.4, 0.1)
    st.divider()
    st.caption("Re-running an invoice that was already processed is correctly rejected as a duplicate. Reset the database to start over.")
    if st.button("Reset demo database", width="stretch"):
        init_db()
        st.session_state.pop("last", None)
        st.success("Database reset. History is empty.")
    if st.button("Run all 12 samples (resets first)", width="stretch"):
        init_db()
        st.session_state.pop("last", None)
        bar, counts = st.progress(0.0, text="Starting..."), {}
        for i, r in enumerate(sample_rows):
            bar.progress(i / len(sample_rows), text=f"Processing {r['file']}")
            final = [e for e in pipeline.run(SAMPLES / r["file"], offline=offline) if e.decision][-1].decision
            counts[final.outcome.value] = counts.get(final.outcome.value, 0) + 1
        bar.progress(1.0, text="Done")
        st.success(" · ".join(f"{k}: {v}" for k, v in counts.items()))
        st.page_link("pages/1_Dashboard.py", label="Open the dashboard", icon="📊")

# ------------------------------------------------------------------ header + input
st.title("🧾 Invoice processing: from PDF to decision")
st.caption("Every stage below runs for real. The AI reads the document; fixed, auditable business rules make the decision.")

src = st.radio("Invoice source", ["Sample invoice", "Upload a PDF"], horizontal=True, label_visibility="collapsed")
path = None
if src == "Sample invoice":
    row = st.selectbox("Choose a sample invoice", sample_rows, format_func=lambda r: f"{r['file']}  ·  {r['edge_case']}")
    path = SAMPLES / row["file"]
else:
    up = st.file_uploader("Upload an invoice PDF", type="pdf")
    if up is not None:
        UPLOADS.mkdir(parents=True, exist_ok=True)
        path = UPLOADS / up.name
        path.write_bytes(up.getbuffer())
        if offline:
            st.warning("Offline mode only knows the sample invoices. Turn it off in the sidebar to read your own PDF with Gemini.")

run_clicked = st.button("▶  Run pipeline", type="primary", disabled=path is None)


# ------------------------------------------------------------------ painting
def paint(events_iter, pdf_path: Path):
    """Draw document + stage list; consume events (live generator OR stored list) and update each stage in place."""
    left, right = st.columns([2, 3], gap="large")
    with left:
        st.image(pdf_png(pdf_path), caption=pdf_path.name, width="stretch")
    with right:
        banner_slot = st.empty()
        slots = {k: st.empty() for k in pipeline.STAGE_LABELS}
        for k, slot in slots.items():
            slot.markdown(stage_card_html(pipeline.STAGE_LABELS[k], "pending"), unsafe_allow_html=True)
        collected = []
        for ev in events_iter:
            collected.append(ev)
            slots[ev.stage].markdown(
                stage_card_html(pipeline.STAGE_LABELS[ev.stage], event_state(ev.status, ev.check), ev.detail, ev.duration_ms),
                unsafe_allow_html=True)
        final = collected[-1].decision if collected else None
        if final:
            banner_slot.markdown(banner_html(final), unsafe_allow_html=True)
    return collected


def show_details(events):
    final = next((e.decision for e in reversed(events) if e.decision), None)
    extract_ev = next((e for e in events if e.stage == "extract" and e.data), None)
    inv = Invoice.model_validate(extract_ev.data["invoice"]) if extract_ev else None
    if final:
        st.divider()
        render_details(final, inv)


if run_clicked and path is not None:
    events = paint(pipeline.run(path, offline=offline, step_delay=delay), path)
    st.session_state["last"] = {"path": str(path), "events": events}
    show_details(events)
elif st.session_state.get("last"):
    last = st.session_state["last"]
    events = paint(iter(last["events"]), Path(last["path"]))
    show_details(events)
else:
    st.markdown("##### The process, end to end")
    cols = st.columns(2)
    for i, (k, label) in enumerate(pipeline.STAGE_LABELS.items()):
        cols[i % 2].markdown(stage_card_html(label, "pending"), unsafe_allow_html=True)
    st.caption("Pick an invoice above and press Run to watch these stages execute.")
