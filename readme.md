# Invoice Processing Agent: from PDF to decision

An accounts-payable process that takes a vendor invoice PDF and returns a clear, explained decision:
**Approved**, **Flagged for human review**, or **Rejected**, with every step visible along the way.

> **Design principle: the AI reads, fixed rules decide.**
> An LLM (Gemini) turns messy PDFs into structured data. It never approves or rejects anything.
> Every decision comes from transparent, testable business rules, and each rule explains itself in plain English.

## The problem in one paragraph

A mid-size company gets hundreds of vendor invoices a month. Someone has to open each one, find the matching
purchase order (PO), check that the numbers line up, and decide what to do. It is slow, repetitive, and a tired
person on a Friday afternoon makes expensive mistakes. This project automates the routine cases, and, just as
importantly, **stops and asks a person** whenever something looks off.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then put your key in .env:  GEMINI_API_KEY=...
streamlit run app.py
```

- The mock procurement database seeds itself on first launch.
- **No API key? It still runs.** The 12 sample invoices have stored extractions (toggle _Offline mode_ in the sidebar).
- Uploading your **own** PDF needs a Gemini key (free tier works).
- `python run_cli.py --all --offline` runs all 12 samples in the terminal. `python -m pytest -q` runs the test suite.

## What you will see

| Page                    | What it shows                                                                                                                                                                                                                                        |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Live Run** (`app.py`) | The invoice on the left; 12 stages executing in real time on the right; a decision banner with the reasoning; what the system read (with confidence); invoice lines vs PO prices; how much of the PO has been billed; the full check-by-check trail. |
| **Dashboard**           | KPIs (processed, approved, flagged, rejected, value awaiting review), outcome and "why invoices need attention" charts, a filterable run history with CSV export, and a drill-down into any past run.                                                |

## How it works

```
PDF -> 1 Ingest -> 2 Extract (LLM) -> 3 Completeness -> 4 Arithmetic -> 5 Vendor -> 6 Duplicates
    -> 7 PO match -> 8 Prices vs PO -> 9 PO balance -> 10 Tax -> 11 Bank account -> 12 Decision
```

1. **Ingest.** Fingerprint the file, detect text PDF vs scanned image.
2. **Extract.** Gemini reads the PDF (text or scanned) into a typed schema: every field carries a value, a confidence, and the text it came from. Missing fields stay empty; nothing is guessed.
3. **Completeness.** Are the critical fields (vendor, invoice number, date, total, line items) present and confidently read?
4. **Arithmetic.** Do the invoice's own lines, subtotal, tax and total add up?
5. **Vendor.** Is the supplier on the approved vendor list (fuzzy name match)?
6. **Duplicates.** Same vendor + invoice number already processed (reject)? Or the same money and items re-keyed under a new number (flag)?
7. **PO match.** Use the PO reference if present; otherwise suggest the best-fitting open PO for that vendor.
8. **Prices vs PO.** Unit price per line against the PO, with a tolerance.
9. **PO balance.** Partial billing is normal; billing more than the PO allows is not. Earlier approved invoices count.
10. **Tax.** Stated tax vs the rate recorded on the PO.
11. **Bank account.** Remittance account vs the one on file for the vendor.
12. **Decision.** Combine the findings by severity and write the explanation.

### Decision rules

| Finding                                                                                       | Outcome                                             |
| --------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| Any **hard-stop** failure (an exact duplicate invoice, or a vendor who is explicitly blocked) | **REJECTED**                                        |
| Any **needs-a-person** failure (see edge cases)                                               | **FLAGGED**, with reasons and a suggested next step |
| Everything passes, or only small in-tolerance variances                                       | **APPROVED** (small variances are noted)            |

Higher severity always wins, so one clean check can never outvote a fraud signal.
All thresholds (price tolerance, confidence limit, duplicate window, PO-match scoring) live in `config/policy.yaml`.

## Edge cases (and why they matter)

| #   | Scenario                                                                                                                                          | Behaviour                                                                                                                                                     | Business reasoning                                                                                                         |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 1   | **Split invoicing against one PO.** A $10,000 PO billed in three stages.                                                                          | Stages 1 and 2 approve and consume the PO. Stage 3 bills 32 hours when only 25 remain: flagged, showing the $700 overrun.                                     | Vendors legitimately bill in stages; checking each invoice alone either blocks real invoices or lets over-billing through. |
| 2   | **Bank account changed.** Everything else is perfect, but the payment account differs from the vendor master.                                     | Always flagged as high risk; never auto-approved. Suggested action: call the vendor on the number in the vendor master.                                       | Classic invoice-fraud pattern. One failing fraud signal must outweigh any number of passing checks.                        |
| 3   | **No PO reference.**                                                                                                                              | Finds the vendor's best-fitting open PO by item descriptions and prices, shows the score and runner-up, and flags it as a _suggestion_ a person must confirm. | Saves the reviewer the search, but a guess must never move money.                                                          |
| 4   | **Near-duplicate resubmission.** Same vendor, amount and items as a paid invoice, but a re-keyed number (`INV-2041` vs `INV2041-R`) and new date. | Flagged as a probable duplicate.                                                                                                                              | Exact-match duplicate checks miss exactly the cases that cost real money.                                                  |

Also covered: a **scanned** (image-only) invoice, **missing fields** (no invoice number or date), an **unknown vendor** (held pending onboarding rather than rejected, since a new supplier is not necessarily fraud),
and a **small in-tolerance price variance** (+1.1%) that is accepted with a note.

## The 12 sample invoices

Run in this order on a freshly reset database (`expected.csv` has the full list with the check that should trigger):

| Invoice                            | Expected                                                    |
| ---------------------------------- | ----------------------------------------------------------- |
| `clean_01`, `clean_02`, `clean_03` | Approved (three layouts; `clean_03` has a +1.1% price WARN) |
| `scanned_01`                       | Approved (image-only PDF)                                   |
| `missing_fields_01`                | Flagged (no invoice number or date)                         |
| `split_A`, `split_B`               | Approved                                                    |
| `split_C`                          | Flagged (PO overrun)                                        |
| `bank_change_01`                   | Flagged (bank details changed)                              |
| `no_po_01`                         | Flagged (suggests PO-1005)                                  |
| `dup_resubmit_01`                  | Flagged (probable duplicate)                                |
| `unapproved_vendor_01`             | Flagged (unknown supplier: hold pending onboarding)         |

To see a **Rejected** outcome, run any invoice a second time: an exact duplicate is a hard stop. Use **Reset demo database** to start over.

## Project layout

```
app.py                  Live Run View (Streamlit)
pages/1_Dashboard.py    History and analytics
ui.py                   Shared styling and rendering
engine/
  schemas.py            Typed data contracts (invoice, check result, decision, events)
  extract.py            Gemini extraction + cache + stored-extraction fallback
  checks.py             The business rules (no LLM)
  decide.py             Severity precedence + plain-English explanation
  pipeline.py           Streams stage events; saves runs
config/policy.yaml      Every threshold, editable
data/                   Mock ERP seed, sample PDFs, stored extractions, expected outcomes
tests/                  20 tests: outcome AND reason for every sample invoice
run_cli.py              Terminal runner
```

## Reliability notes

- **Extraction order:** cache (same file seen before) -> Gemini (tries each model in `policy.yaml` in turn) -> stored extraction. If Gemini is unavailable, the run says so on screen; it never silently pretends.
- **If a document cannot be read at all,** the invoice is flagged for a person with the reason, not crashed or guessed.
- Bank account numbers are only ever shown as their last four digits.

## Assumptions and limits (honest list)

- **Two-way match only** (invoice vs PO). A three-way match against goods-received records is the natural next step.
- Tolerances (2% auto-accept, 5% high-risk) and the tax-from-PO rule are **assumptions** I chose, and they are configurable. Real values should be calibrated on historical invoices.
- The model's self-reported confidence is weak evidence on its own, so it is combined with arithmetic and master-data cross-checks.
- Data is synthetic. One currency (USD). The UI previews the first page of a PDF.
- Flagged invoices are explained and routed, but there is no reviewer approve/reject workflow yet.

## What I would build next

1. Reviewer actions (approve / reject / reassign PO) feeding back into the rules, so the system learns which flags were unnecessary.
2. Three-way match with goods-received data; multi-currency; multi-page and multi-invoice PDFs.
3. ERP integration, and tolerance calibration from historical data.
4. Metrics on straight-through rate, false-approval rate, and reviewer minutes saved.

## Deployment notes

Deployed on Streamlit Community Cloud with two secrets: `GEMINI_API_KEY` and `PER_SESSION_DB = "1"`.
With `PER_SESSION_DB=1` every visitor gets a private copy of the mock ERP, so concurrent reviewers never
collide (locally, a single shared `data/ap.db` is used). The 12 sample invoices work from the shipped
`.cache/` without any live call; only a brand-new uploaded PDF needs the Gemini key.
