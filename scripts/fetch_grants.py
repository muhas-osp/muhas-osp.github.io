#!/usr/bin/env python3
"""
fetch_grants.py — Pull OSP-verified funding opportunities from REDCap and write
data/grants.json for the static "Funding Opportunities" board.
Standard library only.

Publishing rules
----------------
A record is published only when ALL of these hold:
  * osp_status is "Verified - publish"   (unless PUBLISH_UNVERIFIED=1)
  * tz_eligibility is not "Not eligible"
  * funder_name, grant_title and application_url are present
  * deadline_type is "Rolling", OR at least one deadline round is today or later

Only whitelisted, researcher-facing fields are written: osp_notes, source_text,
entry_method and the like never reach the (public) repository.

Environment variables
---------------------
Required:
  REDCAP_GRANTS_TOKEN  project token for an API user with *export-only* rights
Optional:
  REDCAP_API_URL       default https://utafiti.muhas.ac.tz/api/
  REDCAP_FORM          instrument name (default: grant_details)
  GRANTS_OUTPUT        output path (default: data/grants.json)
  PORTAL_TZ            timezone that defines "today" (default: Africa/Dar_es_Salaam)
  PUBLISH_UNVERIFIED   "1" to publish regardless of OSP status (testing only)

Exit codes: 0 success, 1 configuration error, 2 API error.
On any error the existing grants.json is left untouched.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger("fetch_grants")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API_URL = "https://utafiti.muhas.ac.tz/api/"

REQUIRED_FIELDS = ("funder_name", "grant_title", "application_url")
DEADLINE_FIELDS = ("deadline_date",) + tuple(f"deadline_date_{i}" for i in range(2, 7))


# --------------------------------------------------------------------------- #
# REDCap access (standard library only, like the DCEPD catalogue scripts)
# --------------------------------------------------------------------------- #
def _post(api_url: str, body: dict[str, str], attempts: int = 4) -> Any:
    """POST to the REDCap API with simple backoff on network / 5xx errors."""
    data = urlencode(body).encode()
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = Request(api_url, data=data, headers={"User-Agent": "muhas-funding-board/1.0"})
            with urlopen(req, timeout=180) as resp:
                return json.load(resp)
        except HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            if exc.code < 500 and exc.code != 429:
                raise RuntimeError(f"REDCap API HTTP {exc.code}: {detail}") from None
            last = exc
        except (URLError, TimeoutError, ValueError) as exc:
            last = exc
        time.sleep(2 ** i)
    raise RuntimeError(f"REDCap API unreachable after {attempts} attempts: {last}")


def fetch_records(api_url: str, token: str, form: str) -> list[dict[str, Any]]:
    """Export one instrument as flat JSON with labels.

    rawOrLabel=label + exportCheckboxLabel=true: checkbox columns
    (career_stage___1, ...) hold the option label or "", dropdowns/radios
    hold their label.
    """
    data = _post(api_url, {
        "token": token,
        "content": "record",
        "action": "export",
        "format": "json",
        "type": "flat",
        "forms[0]": form,
        "rawOrLabel": "label",
        "rawOrLabelHeaders": "raw",
        "exportCheckboxLabel": "true",
        "exportSurveyFields": "false",
        "exportDataAccessGroups": "false",
        "returnFormat": "json",
    })
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"REDCap API error: {data['error']}")
    if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
        raise RuntimeError("REDCap did not return a valid record list")
    return data


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _checkbox(rec: dict[str, Any], field: str) -> list[str]:
    """Selected labels of a checkbox field, in choice-code order."""
    prefix = f"{field}___"
    cols = [k for k in rec if k.startswith(prefix)]

    def order(col: str):
        s = col[len(prefix):]
        return (0, int(s), "") if s.isdigit() else (1, 0, s)

    return [_clean(rec[c]) for c in sorted(cols, key=order) if _clean(rec[c])]


def _amount(raw: Any) -> int | float | None:
    text = str(raw or "").replace(",", "").strip()
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def _amount_display(amount: int | float | None, currency: str) -> str:
    if amount is None:
        return ""
    fmt = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
    return f"Up to {currency} {fmt}".replace("  ", " ")


def _safe_url(raw: Any) -> str | None:
    """Absolute http(s) only — blocks javascript:/data: hrefs."""
    url = str(raw or "").strip()
    p = urlparse(url)
    return url if p.scheme in ("http", "https") and p.netloc else None


def _parse_date(raw: Any) -> date | None:
    text = str(raw or "").strip()
    try:
        return date.fromisoformat(text[:10]) if text else None
    except ValueError:
        return None


def _starts(value: Any, prefix: str) -> bool:
    return _clean(value).lower().startswith(prefix)


# --------------------------------------------------------------------------- #
# Core transform
# --------------------------------------------------------------------------- #
def publishable_grants(
    records: list[dict[str, Any]],
    today: date,
    require_verified: bool = True,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    for rec in records:
        rid = _clean(rec.get("record_id")) or "?"

        if require_verified and not _starts(rec.get("osp_status"), "verified"):
            log.info("record %s: not published (OSP status %r)", rid, rec.get("osp_status"))
            continue
        if _starts(rec.get("tz_eligibility"), "not eligible"):
            log.info("record %s: not published (not eligible for Tanzania/MUHAS)", rid)
            continue

        missing = [f for f in REQUIRED_FIELDS if not _clean(rec.get(f))]
        if missing:
            log.warning("record %s: skipped, missing %s", rid, ", ".join(missing))
            continue

        url = _safe_url(rec.get("application_url"))
        if url is None:
            log.warning("record %s: skipped, invalid application_url %r",
                        rid, rec.get("application_url"))
            continue

        rolling = _starts(rec.get("deadline_type"), "rolling")
        rounds = sorted({d for f in DEADLINE_FIELDS if (d := _parse_date(rec.get(f)))})
        upcoming = [d.isoformat() for d in rounds if d >= today]

        if not rolling:
            if not rounds:
                log.warning("record %s: skipped, fixed-deadline call with no valid dates", rid)
                continue
            if not upcoming:
                log.info("record %s: all rounds closed (last %s)", rid, rounds[-1].isoformat())
                continue

        amount = _amount(rec.get("max_funding_amount"))
        currency = _clean(rec.get("funding_currency"))
        opening = _parse_date(rec.get("opening_date"))
        tz = _clean(rec.get("tz_eligibility")) or "Pending assessment"

        out.append({
            "id": rid,
            "intake_id": _clean(rec.get("intake_id")),
            "call_reference": _clean(rec.get("call_reference")),
            "funder_name": _clean(rec["funder_name"]),
            "grant_title": _clean(rec["grant_title"]),
            "application_url": url,
            "snapshot_summary": _clean(rec.get("snapshot_summary")),
            "research_area": _clean(rec.get("research_area")),
            "tz_eligibility": tz,
            "eligible_regions": _checkbox(rec, "eligible_regions"),
            "career_stages": _checkbox(rec, "career_stage"),
            "eligible_applicants": _clean(rec.get("eligible_applicants")),
            "award_budget": _clean(rec.get("award_budget_text")) or _amount_display(amount, currency),
            "max_funding_amount": amount,
            "funding_currency": currency,
            "award_period": _clean(rec.get("award_period")),
            "opening_date": opening.isoformat() if opening else None,
            "rolling": rolling,
            "deadlines": upcoming,                 # all future rounds, ascending
            "next_deadline": upcoming[0] if upcoming else None,
            "deadline_note": _clean(rec.get("deadline_note")),
        })

    # Fixed deadlines first (soonest first), rolling calls last; deterministic.
    out.sort(key=lambda g: (g["rolling"], g["next_deadline"] or "9999-12-31",
                            g["funder_name"].lower(), g["grant_title"].lower()))
    return out


def write_json_atomic(path: Path, data: Any) -> bool:
    """Write only if content changed. Returns True when the file was updated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == new:
        return False
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".grants-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    api_url = os.environ.get("REDCAP_API_URL", "").strip() or DEFAULT_API_URL
    token = os.environ.get("REDCAP_GRANTS_TOKEN", "").strip()
    if not token:
        # not configured yet (e.g. first deployment): keep the current data and let the site deploy
        log.warning("REDCAP_GRANTS_TOKEN is not set; existing grants.json kept unchanged")
        return 0

    form = os.environ.get("REDCAP_FORM", "grant_details")
    output = Path(os.environ.get("GRANTS_OUTPUT", ROOT / "data/grants.json"))
    today = datetime.now(ZoneInfo(os.environ.get("PORTAL_TZ", "Africa/Dar_es_Salaam"))).date()
    require_verified = os.environ.get("PUBLISH_UNVERIFIED", "0") != "1"

    try:
        records = fetch_records(api_url, token, form)
    except RuntimeError as exc:
        log.error("fetch failed: %s", exc)  # token is never logged
        return 2

    grants = publishable_grants(records, today, require_verified)
    changed = write_json_atomic(output, grants)
    log.info("%d records fetched, %d published as of %s; %s %s",
             len(records), len(grants), today.isoformat(),
             "wrote" if changed else "unchanged", output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
