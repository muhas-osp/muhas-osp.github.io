#!/usr/bin/env python3
"""
import_catalogue.py — Convert an OSP funding-opportunities spreadsheet (.xlsx)
into a CSV for REDCap's Data Import Tool, matching grants_data_dictionary.csv.

Handles both layouts the OSP has used:
  * one header row with a "Funder" column (Funding opportunities_Oct 2026.xlsx)
  * funder section rows ("NIH FUNDING OPPORTUNITY", ...) each followed by a
    header row, or reusing the previous header (Grant training_..._Nov 2024.xlsx)

Every imported record is set to osp_status = 1 (Pending OSP verification) and
entry_method = 2 (Spreadsheet import), whatever the sheet says, so nothing goes
public until someone verifies it in REDCap. The sheet's own status is kept in
osp_notes. Messy deadline text ("1. 26/02/2025  2. 08/04/2025",
"15 December 2026 (NIH listing ...)", "Apply anytime") is parsed into
deadline rounds; the original wording is kept in osp_notes for checking.

Requires: pip install openpyxl   (admin machine only; CI doesn't need it)

Usage:
  python import_catalogue.py "Funding opportunities_Oct 2026.xlsx" -o import.csv
  python import_catalogue.py old.xlsx new.xlsx -o import.csv --start-id 100

Then: REDCap > Data Import Tool > upload import.csv (CSV, rows = records,
dates Y-M-D). Review the preview before committing.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date, datetime
from pathlib import Path


# ---- choice codes (must match grants_data_dictionary.csv) ------------------ #
CAREER = {"early": "1", "mid": "2", "senior": "3", "established": "3", "any": "4"}
TZ = {"eligible": "1", "not eligible": "2"}           # else 3 = Pending
FUNDER_ALIASES = {
    "nih": "National Institutes of Health",
    "welcome trust": "Wellcome",
    "wellcome trust": "Wellcome",
    "wellcome": "Wellcome",
    "ukri": "UKRI",
}
PLACEHOLDERS = ("not confirmed", "not specified", "pending assessment", "osp to confirm", "n/a", "tbc")

# Header text (lower-cased, whitespace-collapsed) -> REDCap field
COLUMNS = {
    "opportunity link": "application_url",
    "grant opportunity": "application_url",
    "funding opportunity title": "grant_title",
    "funder": "funder_name",
    "research area": "research_area",
    "opening date": "opening_date",
    "open date": "opening_date",
    "deadline (as published)": "_deadline_raw",
    "deadline date": "_deadline_raw",
    "eligible individuals / institutions": "eligible_applicants",
    "eligible individuals": "eligible_applicants",
    "award budget": "award_budget_text",
    "award period": "award_period",
    "career stage": "_career_raw",
    "tanzania / muhas eligibility": "_tz_raw",
    "osp verification status": "_status_raw",
    "intake id": "intake_id",
    "call reference": "call_reference",
    "foa number": "call_reference",
    "osp notes": "osp_notes",
}

FIELDS = (["record_id", "intake_id", "call_reference", "funder_name", "grant_title",
           "application_url", "snapshot_summary", "research_area", "tz_eligibility",
           "eligible_applicants", "award_budget_text", "max_funding_amount",
           "funding_currency", "award_period", "opening_date", "deadline_type",
           "deadline_date"] + [f"deadline_date_{i}" for i in range(2, 7)]
          + ["deadline_note", "osp_status", "osp_notes", "entry_method"]
          + [f"career_stage___{c}" for c in ("1", "2", "3", "4")])

# ---- text helpers ---------------------------------------------------------- #
def clean(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = " ".join(str(v).replace("•", " ").split())
    return "" if s.lower().startswith(PLACEHOLDERS) else s


MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
         r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
DATE_RE = re.compile(
    rf"(?P<iso>\b\d{{4}}-\d{{2}}-\d{{2}}\b)"
    rf"|(?P<dmy>\b\d{{1,2}}/\d{{1,2}}/\d{{4}}\b)"
    rf"|(?P<dmon>\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH}\.?,?\s+\d{{4}}\b)"
    rf"|(?P<mond>\b{MONTH}\.?,?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b)",
    re.IGNORECASE,
)


def _to_date(text: str) -> date | None:
    t = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text.replace(",", " ").replace(".", " "))
    t = " ".join(t.split())
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    if t.lower().startswith("sept"):
        return _to_date(t[:3] + t[4:])
    return None


def parse_dates(raw) -> list[date]:
    if isinstance(raw, (datetime, date)):
        return [raw.date() if isinstance(raw, datetime) else raw]
    out = []
    for m in DATE_RE.finditer(str(raw or "")):
        d = _to_date(m.group(0))
        if d and d not in out:
            out.append(d)
    return sorted(out)


def parse_budget(text: str) -> tuple[str, str]:
    """Best-effort (max_amount, currency) from prose; blank when unsure."""
    cur = ("USD" if "$" in text else "GBP" if "£" in text else "EUR" if "€" in text
           else "TZS" if re.search(r"\bTZS\b|\bTSh", text, re.I) else "")
    if not cur:
        return "", ""
    vals = []
    for num, mult in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*(million|m\b|k\b)?", text, re.I):
        v = float(num.replace(",", ""))
        v *= {"million": 1e6, "m": 1e6, "k": 1e3}.get(mult.lower(), 1) if mult else 1
        vals.append(v)
    vals = [v for v in vals if v >= 1000]          # drop "2 years", "1." etc.
    return (f"{max(vals):.0f}", cur) if vals else ("", "")


def funder_from_section(text: str) -> str:
    base = re.sub(r"\s*funding opportunit(y|ies)\s*", "", text, flags=re.I).strip()
    return FUNDER_ALIASES.get(base.lower(), base.title())


# ---- sheet walker ---------------------------------------------------------- #
def rows_from_workbook(path: Path):
    import openpyxl  # admin-only dependency

    wb = openpyxl.load_workbook(path, data_only=True)
    for ws in wb.worksheets:
        header: list[str | None] | None = None
        section_funder = ""
        for row in ws.iter_rows(values_only=True):
            cells = list(row)
            texts = [clean(c).lower() for c in cells]
            if not any(texts):
                continue
            if "funding opportunity title" in texts:            # header row
                header = [COLUMNS.get(t) for t in texts]
                continue
            first = str(cells[0] or "").strip()
            if first.lower().startswith("http"):
                if header is None:
                    print(f"warn: data row before any header in {path.name}", file=sys.stderr)
                    continue
                rec = {k: cells[i] for i, k in enumerate(header) if k and i < len(cells)}
                rec.setdefault("funder_name", section_funder)
                if not clean(rec.get("funder_name")):
                    rec["funder_name"] = section_funder
                yield rec, path.name
            elif "funding opportunit" in first.lower() and sum(bool(t) for t in texts) == 1:
                section_funder = funder_from_section(first)


def firewall_safe(value: str) -> str:
    """The MUHAS server firewall blocks REDCap form saves containing ';'. Use ',' instead."""
    return re.sub(r"\s*;\s*", ", ", value)


def to_redcap(rec: dict, source: str, rid: int) -> dict:
    out = {f: "" for f in FIELDS}
    out["record_id"] = str(rid)
    for k in ("intake_id", "call_reference", "funder_name", "grant_title", "research_area",
              "eligible_applicants", "award_budget_text", "award_period"):
        out[k] = clean(rec.get(k))
    out["application_url"] = str(rec.get("application_url") or "").strip()
    out["funder_name"] = FUNDER_ALIASES.get(out["funder_name"].lower(), out["funder_name"])

    opening = parse_dates(rec.get("opening_date"))
    out["opening_date"] = opening[0].isoformat() if opening else ""

    raw_deadline = rec.get("_deadline_raw")
    raw_text = " ".join(str(raw_deadline or "").split()) if not isinstance(raw_deadline, (date, datetime)) \
        else clean(raw_deadline)
    rolling = bool(re.search(r"anytime|rolling|open call|no deadline", raw_text, re.I))
    rounds = parse_dates(raw_deadline)
    out["deadline_type"] = "2" if rolling and not rounds else "1"
    for i, d in enumerate(rounds[:6]):
        out["deadline_date" if i == 0 else f"deadline_date_{i + 1}"] = d.isoformat()
    paren = re.search(r"\(([^)]*)\)", raw_text)
    if paren and not rolling:
        out["deadline_note"] = paren.group(1)[:200]

    amt, cur = parse_budget(out["award_budget_text"])
    out["max_funding_amount"], out["funding_currency"] = amt, cur

    for word, code in CAREER.items():
        if word in str(rec.get("_career_raw") or "").lower():
            out[f"career_stage___{code}"] = "1"

    tz_raw = clean(rec.get("_tz_raw")).lower()
    out["tz_eligibility"] = next((c for w, c in sorted(TZ.items(), key=lambda x: -len(x[0]))
                                  if tz_raw.startswith(w)), "3")

    notes = [f"Imported from {source}."]
    if clean(rec.get("_status_raw")):
        notes.append(f"Sheet status: {clean(rec['_status_raw'])}.")
    notes.append(f"Deadline as published: {raw_text or '(blank)'}.")
    if len(rounds) > 6:
        notes.append(f"{len(rounds) - 6} extra round(s) not imported.")
    if "explore-nih-opportunities" in out["application_url"]:
        notes.append("Link is NIH search page, not the specific RFA. Replace it before verifying.")
    if clean(rec.get("osp_notes")):
        notes.append(clean(rec["osp_notes"]))
    out["osp_notes"] = " ".join(notes)

    out["osp_status"] = "1"        # Pending — always, regardless of sheet
    out["entry_method"] = "2"      # Spreadsheet import
    return {k: v if k == "application_url" else firewall_safe(v) for k, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xlsx", nargs="+", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=Path("redcap_import.csv"))
    ap.add_argument("--start-id", type=int, default=1, help="first record_id (avoid clashes with existing records)")
    args = ap.parse_args()

    records, rid = [], args.start_id
    for p in args.xlsx:
        for rec, src in rows_from_workbook(p):
            records.append(to_redcap(rec, src, rid))
            rid += 1

    with args.output.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(records)
    print(f"{len(records)} records -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
