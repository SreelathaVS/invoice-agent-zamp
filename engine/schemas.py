"""
schemas.py - the data contracts for the whole AP pipeline.

HOW THE SCHEMA FITS TOGETHER (read this first)
----------------------------------------------
    PDF --LLM--> Invoice --checks--> list[CheckResult] --decide--> Decision
                                                         |
              each stage also emits a PipelineEvent -----+--> Live Run View

Design rules behind these models:
  1. The LLM only FILLS Invoice. It never decides anything.
  2. Every decision is the sum of small CheckResults, so every outcome can
     be explained check by check (this is the "explain it to a buyer" rule).
  3. Nothing is silently guessed: unknown values stay None, and low
     confidence is recorded, so uncertainty becomes visible, not hidden.
"""
from enum import Enum
from typing import Any, Optional, Union

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# 1. EXTRACTION LAYER - what the LLM reads from the PDF
# ---------------------------------------------------------------------------

class ExtractedField(BaseModel):
    """One value read from the invoice, plus how far we should trust it.

    Wrapping every field (instead of storing a bare string) lets us:
      - tell "field missing" (value=None) apart from "field read as 0"
      - show the reviewer the exact text the value came from (source_text)
      - route low-confidence critical fields to a human
    """
    value: Optional[Union[str, float]] = None  # None means "not found on the invoice"
    confidence: float = Field(0.0, ge=0.0, le=1.0)  # LLM self-reported, 0..1.
    # NOTE: self-reported confidence is weak evidence on its own. The checks
    # layer cross-validates with hard maths (do lines add up to the total?).
    source_text: Optional[str] = None  # snippet copied from the PDF as evidence


class LineItem(BaseModel):
    """One row of the invoice table. Fields are optional because bundled
    invoices often omit quantity or unit price."""
    description: str
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None  # line total as printed (may include tax)


class Invoice(BaseModel):
    """Everything we try to read from one invoice PDF.

    Header fields are ExtractedField (value + confidence + evidence);
    line_items are plain rows. Any field may be missing - handling that
    gracefully is a pipeline stage, not a crash.
    """
    vendor_name: ExtractedField = Field(default_factory=ExtractedField)
    invoice_number: ExtractedField = Field(default_factory=ExtractedField)
    invoice_date: ExtractedField = Field(default_factory=ExtractedField)
    po_reference: ExtractedField = Field(default_factory=ExtractedField)  # often absent -> fuzzy match
    currency: ExtractedField = Field(default_factory=ExtractedField)
    subtotal: ExtractedField = Field(default_factory=ExtractedField)  # before tax
    tax: ExtractedField = Field(default_factory=ExtractedField)  # may be embedded in lines
    total: ExtractedField = Field(default_factory=ExtractedField)  # amount payable
    bank_account: ExtractedField = Field(default_factory=ExtractedField)  # remittance details, audited for fraud
    line_items: list[LineItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 2. VALIDATION LAYER - one result per business check
# ---------------------------------------------------------------------------

class CheckStatus(str, Enum):
    PASS = "PASS"  # check satisfied
    WARN = "WARN"  # minor variance, inside tolerance: noted but not blocking
    FAIL = "FAIL"  # check violated: severity decides what happens next
    SKIP = "SKIP"  # not applicable (e.g. no vendor identified, so no bank audit)


class Severity(str, Enum):
    """What a FAIL means for the final outcome. This is the knob the
    decision rules read; it is separate from status on purpose.
    """
    INFO = "info"      # informational only, never changes the outcome
    FLAG = "flag"      # a human should look: forces FLAGGED
    REJECT = "reject"  # hard stop: forces REJECTED


class CheckResult(BaseModel):
    """The unit of explainability. Every check (vendor, duplicate, PO match,
    price, bank...) returns exactly one of these, and the UI renders the full
    list as the reasoning trail."""
    name: str  # e.g. "bank_account_match"
    status: CheckStatus
    severity: Severity = Severity.INFO  # only matters when status is FAIL
    detail: str  # plain-English sentence a non-technical buyer can read
    evidence: dict[str, Any] = Field(default_factory=dict)  # raw numbers, e.g. {"invoice": 3200, "po_remaining": 2500}


# ---------------------------------------------------------------------------
# 3. DECISION LAYER - the final output
# ---------------------------------------------------------------------------

class Outcome(str, Enum):
    APPROVED = "APPROVED"  # safe to pay: every check passed (or only minor WARNs)
    FLAGGED = "FLAGGED"    # FLAGGED FOR REVIEW: the system will not pay or reject
    #                        on its own. A human must confirm. Not an error: it is
    #                        the system correctly saying "I am not sure enough".
    REJECTED = "REJECTED"  # hard fail (e.g. unapproved vendor, exact duplicate)


class Decision(BaseModel):
    """What the pipeline hands back for one invoice.

    Precedence (applied in decide.py): any REJECT-severity failure -> REJECTED;
    else any FLAG-severity failure -> FLAGGED; else APPROVED.
    Higher severity always wins, so one perfect check can never outvote a
    fraud signal.
    """
    outcome: Outcome
    rationale: str  # 2-4 sentence buyer-readable explanation
    checks: list[CheckResult]  # full trail behind the outcome
    matched_po: Optional[str] = None  # PO id used (or suggested, when FLAGGED)

    # --- used when outcome == FLAGGED ----------------------------------
    review_reasons: list[str] = Field(default_factory=list)  # WHY it needs a human (one line each)
    suggested_action: Optional[str] = None  # WHAT the reviewer should do next,
    # e.g. "Call vendor on the number in the vendor master to confirm new bank details."

    @model_validator(mode="after")
    def flagged_must_explain_itself(self):
        # A FLAGGED invoice with no reason would just move the problem to
        # the reviewer. Enforce the contract at the schema level.
        if self.outcome == Outcome.FLAGGED and not (self.review_reasons and self.suggested_action):
            raise ValueError("FLAGGED decisions need review_reasons and suggested_action")
        return self


# ---------------------------------------------------------------------------
# 4. LIVE RUN VIEW - what the UI streams, stage by stage
# ---------------------------------------------------------------------------

class StageStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    WARN = "warn"
    FAILED = "failed"


class PipelineEvent(BaseModel):
    """One tick of the Live Run View. pipeline.run() is a generator that
    yields one of these as each stage starts and finishes; Streamlit just
    renders them as they arrive. The final event carries the Decision."""
    stage: str  # "extract", "vendor_check", "po_match", ...
    status: StageStatus
    detail: str = ""  # short human line shown under the stage name
    duration_ms: Optional[int] = None
    check: Optional[CheckResult] = None  # attached when the stage is a check
    decision: Optional[Decision] = None  # set only on the final event
    data: Optional[dict[str, Any]] = None  # extra payload, e.g. the extracted invoice for display
