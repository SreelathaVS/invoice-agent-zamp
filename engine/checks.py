"""
checks.py - the deterministic business rules. NO LLM in this file.

Each check takes a Ctx and returns ONE CheckResult. Checks run in this order
(pipeline.py), and earlier checks fill in the Ctx that later ones need:

    completeness -> math_consistency -> vendor (sets ctx.vendor)
      -> duplicate -> po_match (sets ctx.po) -> line_prices -> po_balance
      -> tax -> bank_account

A check that cannot run (e.g. no PO found, so no prices to compare) returns
SKIP with the reason, instead of failing twice for the same root cause.
Severity says what a FAIL means: FLAG = human review, REJECT = hard stop.
"""
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from rapidfuzz import fuzz

from .schemas import CheckResult, CheckStatus as S, Invoice, Severity as V

MIN_LINE_MATCH = 0.60  # a line "is on the PO" if its description matches at least this well


@dataclass
class Ctx:
    inv: Invoice
    pol: dict                       # policy.yaml
    db: sqlite3.Connection
    vendor: Optional[sqlite3.Row] = None
    po: Optional[sqlite3.Row] = None
    po_lines: list = field(default_factory=list)
    po_method: Optional[str] = None  # "explicit" | "fuzzy"
    line_pairs: list = field(default_factory=list)  # [(invoice line, PO line)] set by line_prices


# ------------------------------------------------------------------ helpers
def money(v: float) -> str:
    return f"${v:,.2f}"


def norm_id(s) -> str:
    """'INV-2041' and 'INV2041-R' -> comparable strings (letters+digits only)."""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper()) if s else ""


def norm_po(s) -> Optional[str]:
    m = re.search(r"PO\D*(\d+)", str(s).upper()) if s else None
    return f"PO-{m.group(1)}" if m else None


def num(f) -> Optional[float]:
    return float(f.value) if f.value not in (None, "") else None


def _stem(w: str) -> str:
    """Crude stemmer so 'boxed lunches' ~ 'lunch boxes' (plurals / tense endings ignored)."""
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _tokens(text: str) -> str:
    return " ".join(_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()))


def sim(a: str, b: str) -> float:
    """Description similarity 0..1, order- and plural-insensitive."""
    return fuzz.token_set_ratio(_tokens(a), _tokens(b)) / 100


def best_line(desc: str, po_lines) -> tuple:
    """Which PO line does this invoice line correspond to? -> (po_line | None, score)."""
    best, best_s = None, 0.0
    for pl in po_lines:
        s = sim(desc, pl["description"])
        if s > best_s:
            best, best_s = pl, s
    return (best, best_s) if best_s >= MIN_LINE_MATCH else (None, best_s)


def line_amount(li) -> float:
    return li.amount if li.amount is not None else (li.quantity or 0) * (li.unit_price or 0)


def res(name, status, detail, severity=V.INFO, **evidence) -> CheckResult:
    return CheckResult(name=name, status=status, severity=severity, detail=detail, evidence=evidence)


# ------------------------------------------------------------------ 1. completeness
def completeness(ctx: Ctx) -> CheckResult:
    """Are the critical fields present and confidently read? Never guess a missing one."""
    pol = ctx.pol["confidence"]
    missing, low = [], []
    for name in pol["critical_fields"]:
        f = getattr(ctx.inv, name)
        label = name.replace("_", " ")
        if f.value in (None, ""):
            missing.append(label)
        elif f.confidence < pol["min_critical_field"]:
            low.append(f"{label} ({f.confidence:.0%} confident)")
    if not ctx.inv.line_items:
        missing.append("line items")
    if missing or low:
        parts = []
        if missing: parts.append("missing " + ", ".join(missing))
        if low: parts.append("hard to read: " + ", ".join(low))
        return res("completeness", S.FAIL, "Critical information is incomplete: " + "; ".join(parts) + ".",
                   V.FLAG, missing=missing, low_confidence=low)
    return res("completeness", S.PASS, "All critical fields (vendor, invoice number, date, total, line items) were found and read confidently.")


# ------------------------------------------------------------------ 2. arithmetic
def math_consistency(ctx: Ctx) -> CheckResult:
    """Do the invoice's own numbers add up? Catches vendor errors and extraction errors alike."""
    tol = ctx.pol["tolerance"]["total_abs"]
    total, sub, tax = num(ctx.inv.total), num(ctx.inv.subtotal), num(ctx.inv.tax) or 0.0
    if total is None:
        return res("math_consistency", S.SKIP, "No total was extracted, so the arithmetic cannot be checked.")
    lines_sum = round(sum(line_amount(li) for li in ctx.inv.line_items), 2)
    sub = lines_sum if sub is None else sub
    issues = []
    for li in ctx.inv.line_items:
        if li.quantity and li.unit_price and li.amount is not None and abs(li.quantity * li.unit_price - li.amount) > tol:
            issues.append(f"line '{li.description}': {li.quantity:g} x {money(li.unit_price)} is not {money(li.amount)}")
    if abs(lines_sum - sub) > tol:
        issues.append(f"line items add up to {money(lines_sum)} but subtotal says {money(sub)}")
    if abs(sub + tax - total) > tol:
        issues.append(f"subtotal {money(sub)} + tax {money(tax)} = {money(sub + tax)}, but total says {money(total)}")
    if issues:
        return res("math_consistency", S.FAIL, "The invoice does not add up: " + "; ".join(issues) + ".", V.FLAG, issues=issues)
    return res("math_consistency", S.PASS, f"Line items, subtotal ({money(sub)}), tax ({money(tax)}) and total ({money(total)}) are consistent.")


# ------------------------------------------------------------------ 3. vendor
def vendor(ctx: Ctx) -> CheckResult:
    """Is this an approved supplier? Fuzzy name match against the vendor master."""
    name = ctx.inv.vendor_name.value
    if not name:
        return res("vendor", S.FAIL, "No vendor name could be read, so the supplier cannot be verified.", V.FLAG)
    rows = ctx.db.execute("SELECT * FROM vendors").fetchall()
    scored = sorted(((fuzz.WRatio(str(name).lower(), r["name"].lower()), r) for r in rows), key=lambda t: -t[0])
    score, best = scored[0]
    if score >= 88:
        ctx.vendor = best
        if best["approved"]:
            return res("vendor", S.PASS, f"'{name}' is an approved vendor ({best['vendor_id']}, {best['name']}).", vendor_id=best["vendor_id"], match_score=round(score))
        return res("vendor", S.FAIL, f"'{best['name']}' exists but is NOT approved for purchasing.", V.REJECT, vendor_id=best["vendor_id"])
    return res("vendor", S.FAIL, f"'{name}' is not on the approved vendor list (closest match: {best['name']}, {score:.0f}% similar).",
               V.REJECT, closest=best["name"], match_score=round(score))


# ------------------------------------------------------------------ 4. duplicates
def duplicate(ctx: Ctx) -> CheckResult:
    """Exact duplicate -> reject. Near duplicate (re-keyed number, same money) -> flag."""
    if not ctx.vendor:
        return res("duplicate", S.SKIP, "Vendor unknown, so there is no payment history to compare against.")
    pol = ctx.pol["duplicate"]
    num_ = norm_id(ctx.inv.invoice_number.value)
    total = num(ctx.inv.total)
    rows = ctx.db.execute("SELECT * FROM invoices WHERE vendor_id=? AND status IN ('APPROVED','FLAGGED')", (ctx.vendor["vendor_id"],)).fetchall()
    if num_:
        for r in rows:
            if norm_id(r["invoice_number"]) == num_:
                return res("duplicate", S.FAIL, f"Invoice {ctx.inv.invoice_number.value} from this vendor was already processed on {r['invoice_date']}.",
                           V.REJECT, earlier_invoice=r["invoice_number"])
    d = ctx.inv.invoice_date.value
    if total is None or not d:
        return res("duplicate", S.SKIP if not num_ else S.PASS,
                   "No earlier invoice with the same number. Near-duplicate test skipped: date or total missing." if num_
                   else "Cannot test for duplicates without an invoice number, date or total.")
    try:
        this_day = date.fromisoformat(str(d))
    except ValueError:
        return res("duplicate", S.PASS, "No earlier invoice with the same number (date unreadable, near-duplicate test skipped).")
    for r in rows:
        try:
            days = abs((this_day - date.fromisoformat(r["invoice_date"])).days)
        except (TypeError, ValueError):
            continue
        amt_pct = abs(total - r["total"]) / r["total"] * 100 if r["total"] else 100
        prior_lines = json.loads(r["lines_json"] or "[]")
        if prior_lines and ctx.inv.line_items:
            line_sim = sum(max(sim(li.description, p["description"]) for p in prior_lines) for li in ctx.inv.line_items) / len(ctx.inv.line_items)
        else:
            line_sim = 0.0
        if days <= pol["near_window_days"] and amt_pct <= pol["amount_tolerance_pct"] and line_sim >= pol["line_similarity_min"]:
            return res("duplicate", S.FAIL,
                       f"Probable duplicate of {r['invoice_number']} ({r['invoice_date']}): same vendor, same amount ({money(total)}), "
                       f"{line_sim:.0%} similar line items, only {days} days apart, but a different invoice number "
                       f"('{ctx.inv.invoice_number.value}' vs '{r['invoice_number']}').",
                       V.FLAG, earlier_invoice=r["invoice_number"], days_apart=days, line_similarity=round(line_sim, 2))
    return res("duplicate", S.PASS, "No earlier invoice from this vendor has the same number, or the same amount and items.")


# ------------------------------------------------------------------ 5. PO matching
def _score_po(inv_lines, po_lines) -> float:
    """How well do the invoice's lines fit this PO? 65% description, 35% unit price."""
    if not inv_lines or not po_lines:
        return 0.0
    d, p = [], []
    for li in inv_lines:
        pl, s = max(((pl, sim(li.description, pl["description"])) for pl in po_lines), key=lambda t: t[1])
        d.append(s)
        if li.unit_price and pl["unit_price"]:
            p.append(1.0 if abs(li.unit_price - pl["unit_price"]) / pl["unit_price"] <= 0.05 else 0.0)
    return 0.65 * sum(d) / len(d) + 0.35 * (sum(p) / len(p) if p else 0.5)


def _load_po(ctx: Ctx, po_row, method: str):
    ctx.po, ctx.po_method = po_row, method
    ctx.po_lines = ctx.db.execute("SELECT * FROM po_lines WHERE po_number=? ORDER BY line_no", (po_row["po_number"],)).fetchall()


def po_match(ctx: Ctx) -> CheckResult:
    """Explicit PO reference -> look it up. No reference -> fuzzy-match to the vendor's open POs."""
    ref = norm_po(ctx.inv.po_reference.value)
    if ref:
        po = ctx.db.execute("SELECT * FROM purchase_orders WHERE po_number=?", (ref,)).fetchone()
        if po is None:
            return res("po_match", S.FAIL, f"The invoice cites {ref}, which does not exist in the procurement system.", V.FLAG, po_ref=ref)
        if ctx.vendor and po["vendor_id"] != ctx.vendor["vendor_id"]:
            return res("po_match", S.FAIL, f"{ref} belongs to a different vendor ({po['vendor_id']}), not {ctx.vendor['name']}.", V.FLAG, po_ref=ref)
        if po["status"] != "OPEN":
            return res("po_match", S.FAIL, f"{ref} is {po['status']}, not open for invoicing.", V.FLAG, po_ref=ref)
        _load_po(ctx, po, "explicit")
        return res("po_match", S.PASS, f"{ref} found, open, and belongs to {ctx.vendor['name'] if ctx.vendor else 'this vendor'}.", po_ref=ref, method="explicit")

    if not ctx.vendor:
        return res("po_match", S.SKIP, "No PO reference and the vendor is unknown, so no PO can be suggested.")
    pol = ctx.pol["po_match"]
    cands = []
    for po in ctx.db.execute("SELECT * FROM purchase_orders WHERE vendor_id=? AND status='OPEN'", (ctx.vendor["vendor_id"],)).fetchall():
        lines = ctx.db.execute("SELECT * FROM po_lines WHERE po_number=?", (po["po_number"],)).fetchall()
        cands.append((round(_score_po(ctx.inv.line_items, lines), 2), po))
    cands.sort(key=lambda t: -t[0])
    top3 = [{"po": p["po_number"], "score": s} for s, p in cands[:3]]
    if not cands or cands[0][0] < pol["fuzzy_min_score"]:
        return res("po_match", S.FAIL, "The invoice has no PO reference and none of this vendor's open POs fit its line items.", V.FLAG, candidates=top3)
    best_s, best_po = cands[0]
    runner = cands[1][0] if len(cands) > 1 else 0.0
    if best_s - runner >= pol["clear_winner_margin"]:
        _load_po(ctx, best_po, "fuzzy")  # downstream checks run against the suggestion so the reviewer sees the full picture
        return res("po_match", S.FAIL,
                   f"No PO reference on the invoice. Best match is {best_po['po_number']} ({best_s:.0%} fit on items and prices; next best {runner:.0%}). "
                   "It is only a suggestion, a person must confirm before payment.", V.FLAG, suggested_po=best_po["po_number"], candidates=top3, method="fuzzy")
    names = ", ".join(f"{c['po']} ({c['score']:.0%})" for c in top3)
    return res("po_match", S.FAIL, f"No PO reference, and several POs fit about equally well: {names}. A person must choose.", V.FLAG, candidates=top3)


# ------------------------------------------------------------------ 6. line prices
def line_prices(ctx: Ctx) -> CheckResult:
    """Compare each invoice line's unit price to the PO. Small drift is tolerated, large drift is flagged."""
    if not ctx.po:
        return res("line_prices", S.SKIP, "No PO matched, so prices cannot be compared.")
    tol = ctx.pol["tolerance"]
    rows, unmatched, ctx.line_pairs = [], [], []
    for li in ctx.inv.line_items:
        pl, score = best_line(li.description, ctx.po_lines)
        if pl is None:
            unmatched.append(li.description)
            continue
        ctx.line_pairs.append((li, pl))
        price = li.unit_price if li.unit_price is not None else (li.amount / li.quantity if li.amount and li.quantity else None)
        if price is None:
            continue
        pct = (price - pl["unit_price"]) / pl["unit_price"] * 100
        rows.append({"item": li.description, "invoice_price": round(price, 2), "po_price": pl["unit_price"], "variance_pct": round(pct, 2)})
    worst = max((abs(r["variance_pct"]) for r in rows), default=0.0)
    if unmatched:
        return res("line_prices", S.FAIL, f"Item(s) not authorised on {ctx.po['po_number']}: {', '.join(unmatched)}.", V.FLAG, unmatched=unmatched, lines=rows)
    if worst > tol["price_flag_pct"]:
        bad = [r for r in rows if abs(r["variance_pct"]) > tol["price_flag_pct"]][0]
        return res("line_prices", S.FAIL, f"Large price difference: '{bad['item']}' is billed at {money(bad['invoice_price'])} vs {money(bad['po_price'])} on the PO ({bad['variance_pct']:+.1f}%).",
                   V.FLAG, risk="high", lines=rows)
    if worst > tol["price_auto_pct"]:
        bad = [r for r in rows if abs(r["variance_pct"]) > tol["price_auto_pct"]][0]
        return res("line_prices", S.FAIL, f"Price above the {tol['price_auto_pct']:g}% auto-accept tolerance: '{bad['item']}' {bad['variance_pct']:+.1f}% vs the PO.", V.FLAG, lines=rows)
    if worst > 0.005:
        r = max(rows, key=lambda r: abs(r["variance_pct"]))
        return res("line_prices", S.WARN, f"Small price variance accepted: '{r['item']}' is {r['variance_pct']:+.1f}% vs the PO (tolerance {tol['price_auto_pct']:g}%).", lines=rows)
    return res("line_prices", S.PASS, "All unit prices match the PO exactly.", lines=rows)


# ------------------------------------------------------------------ 7. PO balance (split invoicing)
def po_balance(ctx: Ctx) -> CheckResult:
    """Partial billing is normal. Billing MORE than the PO authorises is not. Counts earlier approved invoices."""
    if not ctx.po:
        return res("po_balance", S.SKIP, "No PO matched, so the remaining balance cannot be checked.")
    po_no = ctx.po["po_number"]
    po_total = round(sum(pl["quantity"] * pl["unit_price"] for pl in ctx.po_lines), 2)
    prior_amt, prior_qty = 0.0, {pl["id"]: 0.0 for pl in ctx.po_lines}
    for r in ctx.db.execute("SELECT * FROM invoices WHERE po_number=? AND status='APPROVED'", (po_no,)).fetchall():
        prior_amt += r["subtotal"] or 0.0
        for pli in json.loads(r["lines_json"] or "[]"):
            pl, _ = best_line(pli["description"], ctx.po_lines)
            if pl is not None:
                prior_qty[pl["id"]] += pli.get("quantity") or 0.0
    this_amt = num(ctx.inv.subtotal)
    this_amt = round(sum(line_amount(li) for li in ctx.inv.line_items), 2) if this_amt is None else this_amt
    this_qty = {pl["id"]: 0.0 for pl in ctx.po_lines}
    for li, pl in ctx.line_pairs:
        this_qty[pl["id"]] += li.quantity or 0.0
    remaining = round(po_total - prior_amt, 2)
    over = []
    for pl in ctx.po_lines:
        left = pl["quantity"] - prior_qty[pl["id"]]
        if this_qty[pl["id"]] > left + 1e-9:
            over.append(f"'{pl['description']}': {this_qty[pl['id']]:g} billed but only {left:g} of {pl['quantity']:g} remain")
    evidence = dict(po=po_no, po_total=po_total, billed_before=round(prior_amt, 2), remaining_before=remaining, this_invoice=this_amt)
    # An in-tolerance price increase (see line_prices) may push the amount a little past the PO total;
    # that is accepted with a note, as long as no quantity is exceeded.
    slack = po_total * ctx.pol["tolerance"]["price_auto_pct"] / 100
    if this_amt > remaining + slack or over:
        parts = []
        if this_amt > remaining + 0.005:
            parts.append(f"{po_no} has {money(remaining)} left of {money(po_total)}, but this invoice bills {money(this_amt)}, an overrun of {money(this_amt - remaining)}")
        if over:
            parts.append("quantities exceed the PO: " + "; ".join(over))
        return res("po_balance", S.FAIL, ". ".join(p[0].upper() + p[1:] for p in parts) + ".", V.FLAG, overrun=round(max(this_amt - remaining, 0), 2), **evidence)
    if this_amt > remaining + 0.005:
        return res("po_balance", S.WARN, f"Slightly above the remaining {po_no} balance by {money(this_amt - remaining)} "
                   f"(within the {ctx.pol['tolerance']['price_auto_pct']:g}% tolerance, quantities are within the PO).", **evidence)
    pct = (prior_amt + this_amt) / po_total * 100 if po_total else 0
    note = f" ({money(prior_amt)} billed before)" if prior_amt else ""
    return res("po_balance", S.PASS, f"Within the PO balance{note}: this invoice brings {po_no} to {money(prior_amt + this_amt)} of {money(po_total)} ({pct:.0f}%).", **evidence)


# ------------------------------------------------------------------ 8. tax
def tax(ctx: Ctx) -> CheckResult:
    """Stated tax vs the tax rate recorded on the PO."""
    sub = num(ctx.inv.subtotal)
    if not ctx.po or sub is None:
        return res("tax", S.SKIP, "Needs a matched PO and a subtotal to verify the tax.")
    rate = ctx.po["tax_rate"]
    expected, stated = round(sub * rate, 2), num(ctx.inv.tax) or 0.0
    if abs(expected - stated) <= ctx.pol["tolerance"]["tax_abs"]:
        return res("tax", S.PASS, f"Tax {money(stated)} matches the {rate*100:g}% rate on {ctx.po['po_number']}.", expected=expected, stated=stated)
    return res("tax", S.FAIL, f"Tax is {money(stated)} but the {rate*100:g}% rate on {ctx.po['po_number']} implies {money(expected)}.", V.FLAG, expected=expected, stated=stated)


# ------------------------------------------------------------------ 9. bank / remittance audit
def bank_account(ctx: Ctx) -> CheckResult:
    """Pay only to the account we already have on file. A changed account is the classic invoice-fraud signal,
    so it forces human review even when every other check passes. Only the last 4 digits are ever displayed."""
    if not ctx.vendor:
        return res("bank_account", S.SKIP, "Vendor unknown, so there is no bank account on file to compare.")
    on_file = re.sub(r"\D", "", ctx.vendor["bank_account"] or "")
    stated = re.sub(r"\D", "", str(ctx.inv.bank_account.value or ""))
    if not stated:
        return res("bank_account", S.WARN, "The invoice shows no bank details. Payment would go to the account already on file.", on_file_last4=on_file[-4:])
    if stated == on_file or (on_file and on_file in stated):
        return res("bank_account", S.PASS, f"Remittance account (ending {stated[-4:]}) matches the vendor master.", last4=stated[-4:])
    return res("bank_account", S.FAIL, f"PAYMENT DETAILS CHANGED: the invoice asks for payment to an account ending {stated[-4:]}, but the vendor master has one ending {on_file[-4:]}.",
               V.FLAG, risk="high", invoice_last4=stated[-4:], on_file_last4=on_file[-4:])


CHECKS = [
    ("completeness", completeness), ("math_consistency", math_consistency), ("vendor", vendor),
    ("duplicate", duplicate), ("po_match", po_match), ("line_prices", line_prices),
    ("po_balance", po_balance), ("tax", tax), ("bank_account", bank_account),
]
