"""
decide.py - turns the list of CheckResults into ONE Decision.

Precedence (the whole rulebook, on purpose short enough to say out loud):
    any FAIL with severity REJECT  -> REJECTED
    else any FAIL with severity FLAG -> FLAGGED   (needs a human)
    else                              -> APPROVED  (WARNs are noted, not blocking)

Higher severity always wins, so one clean check can never outvote a fraud signal.
The explanation is built from the checks' own plain-English details by template,
not by an LLM, so it can never say something the checks did not find.
"""
from typing import Optional

from .schemas import CheckResult, CheckStatus as S, Decision, Outcome, Severity as V

# What the human reviewer should DO about each kind of finding.
ACTIONS = {
    "completeness": "Get the missing details from the vendor (or find the original document) and resubmit.",
    "math_consistency": "Compare the arithmetic with the PDF; if the vendor made the error, request a corrected invoice.",
    "vendor": "Hold the invoice. Check whether this is a legitimate new supplier and, if so, route them through vendor onboarding (including bank-detail verification) before paying.",
    "duplicate": "Check whether the earlier invoice was already paid; if so, do not pay this one.",
    "po_match": "Confirm the correct purchase order with the requester or vendor and link it before approving.",
    "line_prices": "Ask procurement whether the new price was agreed; otherwise request a credit note.",
    "po_balance": "Check whether the vendor over-billed or the PO needs a top-up approved by the budget owner.",
    "tax": "Verify the tax treatment with finance and ask the vendor to correct the invoice if needed.",
    "bank_account": "Call the vendor on the phone number in the vendor master (NOT any number on the invoice) to verify the new bank details before paying.",
    "extraction": "Open the PDF and enter the invoice data manually.",
}

# Most dangerous findings are listed first for the reviewer.
PRIORITY = ["vendor", "bank_account", "duplicate", "po_balance", "po_match", "line_prices", "tax", "math_consistency", "completeness", "extraction"]


def _rank(c: CheckResult) -> int:
    return PRIORITY.index(c.name) if c.name in PRIORITY else len(PRIORITY)


def decide(checks: list[CheckResult], matched_po: Optional[str] = None) -> Decision:
    fails = [c for c in checks if c.status == S.FAIL]
    rejects = [c for c in fails if c.severity == V.REJECT]
    flags = sorted((c for c in fails if c.severity == V.FLAG), key=_rank)
    warns = [c for c in checks if c.status == S.WARN]
    passed = [c for c in checks if c.status == S.PASS]

    if rejects:
        why = " ".join(c.detail for c in rejects)
        return Decision(outcome=Outcome.REJECTED, checks=checks, matched_po=matched_po,
                        rationale=f"Rejected. {why} This is a hard stop, so the invoice should not be paid.")

    if flags:
        reasons = [c.detail for c in flags]
        actions = []
        for c in flags:
            a = ACTIONS.get(c.name)
            if a and a not in actions:
                actions.append(a)
        action = actions[0] if len(actions) == 1 else " ".join(f"({i}) {a}" for i, a in enumerate(actions, 1))
        first = flags[0].detail
        more = f" {len(flags) - 1} other issue(s) are listed below." if len(flags) > 1 else ""
        rationale = (f"Flagged for human review. {first}{more} The invoice is not clearly invalid, "
                     f"but the system will not pay it without a person confirming. {len(passed)} other check(s) passed.")
        return Decision(outcome=Outcome.FLAGGED, checks=checks, matched_po=matched_po,
                        rationale=rationale, review_reasons=reasons, suggested_action=action)

    note = f" Minor note: {' '.join(c.detail for c in warns)}" if warns else ""
    po = f" against {matched_po}" if matched_po else ""
    return Decision(outcome=Outcome.APPROVED, checks=checks, matched_po=matched_po,
                    rationale=f"Approved for payment. The invoice matched its purchase order{po} and all {len(passed)} applicable checks passed.{note}")