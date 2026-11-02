"""
1_Dashboard.py - HISTORY & OUTCOMES across every run.

KPIs, a filterable run table, "why are invoices getting flagged?" analytics,
and a drill-down that shows the full reasoning for any past run.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt
import pandas as pd
import streamlit as st

from engine.config import connect
from engine.schemas import Decision, Invoice
from ui import banner_html, get_db_path, inject_css, money, outcome_chip, render_details

OUTCOME_COLORS = {"APPROVED": "#16a34a", "FLAGGED": "#d97706", "REJECTED": "#dc2626"}  # same as the decision banners
REASON_NAMES = {  # plain-English names for the buyer
    "bank_account": "Bank details changed", "completeness": "Missing or unclear data", "duplicate": "Duplicate invoice",
    "po_balance": "Exceeds PO balance", "po_match": "PO missing or unclear", "line_prices": "Price differs from PO",
    "tax": "Tax mismatch", "math_consistency": "Invoice arithmetic error", "vendor": "Unknown vendor (on hold)", "extraction": "Document unreadable"}

st.set_page_config(page_title="AP Invoice Agent: Dashboard", page_icon="📊", layout="wide")
inject_css()
DB = get_db_path()
st.title("📊 Dashboard")

con = connect(DB)
raw = pd.read_sql_query("SELECT * FROM runs ORDER BY started_at DESC, rowid DESC", con)
if raw.empty:
    st.info("No runs yet. Process an invoice on the **Live Run** page, or use *Run all 12 samples* in its sidebar.")
    st.stop()


def top_reason(decision_json: str) -> str:
    d = json.loads(decision_json)
    if d["outcome"] == "APPROVED":
        return ""
    bad = [c for c in d["checks"] if c["status"] == "FAIL"]
    bad.sort(key=lambda c: 0 if c["severity"] == "reject" else 1)
    return REASON_NAMES.get(bad[0]["name"], bad[0]["name"]) if bad else ""


def failing_checks(decision_json: str) -> list[str]:
    return [c["name"] for c in json.loads(decision_json)["checks"] if c["status"] == "FAIL"]


rows = []
for r in raw.itertuples():
    inv = json.loads(r.extraction_json) if r.extraction_json else {}
    g = lambda k: (inv.get(k) or {}).get("value")  # noqa: E731
    rows.append({"Run": r.run_id, "Time": r.started_at.replace("T", " "), "File": r.filename, "Vendor": g("vendor_name") or "(unreadable)",
                 "Invoice #": g("invoice_number") or "(missing)", "Total": float(g("total")) if g("total") not in (None, "") else None,
                 "PO": r.matched_po or "", "Outcome": r.outcome, "Main reason": top_reason(r.decision_json),
                 "_fails": failing_checks(r.decision_json)})
df = pd.DataFrame(rows)

# ------------------------------------------------------------------ KPIs
n = len(df)
cnt = df["Outcome"].value_counts()
approved, flagged, rejected = (int(cnt.get(k, 0)) for k in ("APPROVED", "FLAGGED", "REJECTED"))
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Invoices processed", n)
k2.metric("Auto-approved", approved, f"{approved / n:.0%} of total", delta_color="off", delta_arrow="off")
k3.metric("Flagged for review", flagged, f"{flagged / n:.0%} of total", delta_color="off", delta_arrow="off")
k4.metric("Rejected", rejected, f"{rejected / n:.0%} of total", delta_color="off", delta_arrow="off")
k5.metric("Value awaiting review", money(df.loc[df["Outcome"] == "FLAGGED", "Total"].sum()))

# ------------------------------------------------------------------ charts
c1, c2 = st.columns(2, gap="large")
with c1:
    st.markdown("##### Outcomes")
    oc = cnt.reindex(list(OUTCOME_COLORS)).fillna(0).rename_axis("Outcome").reset_index(name="Invoices")
    st.altair_chart(alt.Chart(oc).mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5).encode(
        x=alt.X("Outcome:N", sort=list(OUTCOME_COLORS), axis=alt.Axis(labelAngle=0, title=None)),
        y=alt.Y("Invoices:Q", axis=alt.Axis(tickMinStep=1, title=None)),
        color=alt.Color("Outcome:N", scale=alt.Scale(domain=list(OUTCOME_COLORS), range=list(OUTCOME_COLORS.values())), legend=None),
        tooltip=["Outcome", "Invoices"]).properties(height=290), width="stretch")
with c2:
    st.markdown("##### Why invoices need attention")
    reasons = pd.Series([c for fs in df["_fails"] for c in fs], dtype=str).map(lambda c: REASON_NAMES.get(c, c))
    if reasons.empty:
        st.caption("Nothing flagged or rejected yet.")
    else:
        rc = reasons.value_counts().rename_axis("Reason").reset_index(name="Invoices")
        st.altair_chart(alt.Chart(rc).mark_bar(color="#d97706", cornerRadiusTopRight=5, cornerRadiusBottomRight=5).encode(
            y=alt.Y("Reason:N", sort="-x", axis=alt.Axis(title=None, labelLimit=260)),
            x=alt.X("Invoices:Q", axis=alt.Axis(tickMinStep=1, title=None)),
            tooltip=["Reason", "Invoices"]).properties(height=290), width="stretch")

# ------------------------------------------------------------------ table with filters
st.markdown("##### Run history")
f1, f2 = st.columns([2, 3])
pick = f1.multiselect("Outcome", ["APPROVED", "FLAGGED", "REJECTED"], default=["APPROVED", "FLAGGED", "REJECTED"])
query = f2.text_input("Search vendor, invoice number, file or PO")
view = df[df["Outcome"].isin(pick)]
if query:
    q = query.lower()
    view = view[view[["Vendor", "Invoice #", "File", "PO"]].astype(str).apply(lambda s: s.str.lower().str.contains(q)).any(axis=1)]
st.dataframe(view.drop(columns=["_fails"]), hide_index=True, width="stretch",
             column_config={"Total": st.column_config.NumberColumn(format="$%.2f")})
st.download_button("Download history (CSV)", view.drop(columns=["_fails"]).to_csv(index=False), "invoice_runs.csv", "text/csv")

# ------------------------------------------------------------------ drill-down
st.divider()
st.markdown("##### Inspect a run")
if view.empty:
    st.caption("No runs match the filters.")
    st.stop()
labels = {r["Run"]: f"{r['Time']}  ·  {r['File']}  ·  {r['Outcome']}" for _, r in view.iterrows()}
run_id = st.selectbox("Run", list(labels), format_func=labels.get, label_visibility="collapsed")
row = raw[raw["run_id"] == run_id].iloc[0]
decision = Decision.model_validate_json(row["decision_json"])
inv = Invoice.model_validate_json(row["extraction_json"]) if row["extraction_json"] else None
st.markdown(banner_html(decision), unsafe_allow_html=True)
render_details(decision, inv)