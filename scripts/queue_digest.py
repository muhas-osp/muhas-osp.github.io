#!/usr/bin/env python3
"""
queue_digest.py: weekly funding digest for MUHAS staff, sent by REDCap Alerts.

Builds the digest text from data/grants.json and writes it into the
`funding_digest` instrument of each eligible record in the REDCap staff
directory. A REDCap Alert (logic: digest queued within the last hour and an
email on file) then emails it from utafiti.muhas.ac.tz, piping [email] and the
preferences/unsubscribe survey link itself.

Privacy: this script never requests email or names. It exports only
record_id, category, employment_status, pref_funding, alert_frequency and
unsubscribed_all, and logs counts only.

Who gets it: academic (1), adjunct/honorary (2) or emeritus (3) staff, not
retired/left (status 4), funding alerts not switched off, not unsubscribed.
  weekly (default) : new calls since the last digest + calls closing within 30 days;
                     skipped when both are empty
  monthly          : first Monday-run of the month, all open calls

Environment:
  REDCAP_STAFF_TOKEN   staff-project token: export (those fields) + import (funding_digest)
  REDCAP_API_URL       default https://utafiti.muhas.ac.tz/api/
  DIGEST_DRY_RUN       "1": build and report, write nothing to REDCap or state
  DIGEST_ONLY_RECORDS  comma-separated record_ids (testing: e.g. your own record)
  DIGEST_FORCE         "1": send even when there is nothing new or closing
Exit codes: 0 ok (including nothing to send), 1 configuration error, 2 API error.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fetch_grants", ROOT / "scripts/fetch_grants.py")
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)

TZ = ZoneInfo("Africa/Dar_es_Salaam")
BOARD_URL = "https://muhas-osp.github.io/opportunities/"
CLOSING_DAYS = 30
STAFF_FIELDS = ["record_id", "category", "employment_status", "pref_funding",
                "alert_frequency", "unsubscribed_all"]
ELIGIBLE_CATEGORIES = {"1", "2", "3"}
STATE_FILE = ROOT / "data/digest_state.json"


def annotate(level: str, message: str) -> None:
    print(message, flush=True)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title=Funding digest::{message}", flush=True)


def safe(text: str) -> str:
    """The MUHAS server firewall rejects ';' and markup in REDCap requests."""
    text = re.sub(r"\s*;\s*", ", ", str(text or ""))
    return " ".join(text.replace("<", "(").replace(">", ")").split())


def fmt_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%-d %b %Y")


# --------------------------------------------------------------------------- #
# Digest content (pure functions, tested)
# --------------------------------------------------------------------------- #
def classify(grants: list[dict], sent_ids: set[str], today: date):
    soon = today + timedelta(days=CLOSING_DAYS)
    new = [g for g in grants if g["id"] not in sent_ids]
    closing = [g for g in grants if g.get("next_deadline")
               and date.fromisoformat(g["next_deadline"]) <= soon and g["id"] in sent_ids]
    return new, closing


def entry(g: dict, today: date) -> str:
    lines = [f"* {safe(g['funder_name'])}: {safe(g['grant_title'])}"]
    if g.get("rolling") and not g.get("next_deadline"):
        when = "Rolling, apply any time"
    else:
        days = (date.fromisoformat(g["next_deadline"]) - today).days
        when = f"Deadline {fmt_date(g['next_deadline'])} ({days} days left)"
    extra = [when]
    if g.get("award_budget"):
        extra.append(f"Award: {safe(g['award_budget'])}")
    if g.get("tz_eligibility") and g["tz_eligibility"] != "Eligible":
        extra.append(f"Tanzania eligibility: {safe(g['tz_eligibility'])}")
    lines.append("  " + " | ".join(extra))
    if g.get("snapshot_summary"):
        lines.append("  " + safe(g["snapshot_summary"])[:300])
    lines.append("  " + g["application_url"])
    return "\n".join(lines)


def build_digest(kind: str, grants: list[dict], sent_ids: set[str], today: date) -> tuple[str, str] | None:
    """Returns (subject, body) or None when a weekly digest has nothing to say."""
    week = today.strftime("%-d %b %Y")
    if kind == "monthly":
        if not grants:
            return None
        body = [f"All open funding calls verified by OSP ({len(grants)}):", ""]
        body += [entry(g, today) + "\n" for g in grants]
        subject = f"MUHAS funding opportunities: {len(grants)} open calls ({today.strftime('%B %Y')})"
    else:
        new, closing = classify(grants, sent_ids, today)
        if not new and not closing:
            return None
        body = []
        if new:
            body += [f"NEW THIS WEEK ({len(new)})", ""] + [entry(g, today) + "\n" for g in new]
        if closing:
            body += [f"CLOSING IN THE NEXT {CLOSING_DAYS} DAYS ({len(closing)})", ""] + \
                    [entry(g, today) + "\n" for g in closing]
        parts = [f"{len(new)} new"] if new else []
        parts += [f"{len(closing)} closing soon"] if closing else []
        subject = f"MUHAS funding opportunities: {', '.join(parts)} (week of {week})"
    body += [f"All {len(grants)} open calls, with filters: {BOARD_URL}",
             "Questions or help with an application: Office of Sponsored Programmes, ospmuhas@gmail.com"]
    return safe(subject), "\n".join(body)


def recipients(staff: list[dict], kind: str) -> list[str]:
    """record_ids that should receive this kind of digest (raw-coded export)."""
    out = []
    for r in staff:
        if (r.get("category") not in ELIGIBLE_CATEGORIES or r.get("employment_status") == "4"
                or r.get("pref_funding") == "0" or r.get("unsubscribed_all") == "1"):
            continue
        monthly = r.get("alert_frequency") == "3"
        if (kind == "monthly") == monthly:
            out.append(r["record_id"])
    return out


# --------------------------------------------------------------------------- #
# REDCap I/O
# --------------------------------------------------------------------------- #
def export_staff(api_url: str, token: str) -> list[dict]:
    body = {"token": token, "content": "record", "action": "export", "format": "json",
            "type": "flat", "rawOrLabel": "raw", "returnFormat": "json"}
    body.update({f"fields[{i}]": f for i, f in enumerate(STAFF_FIELDS)})
    data = fg._post(api_url, body)
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"REDCap API error: {data['error']}")
    return data


def import_digests(api_url: str, token: str, rows: list[dict], batch: int = 100) -> int:
    done = 0
    for i in range(0, len(rows), batch):
        res = fg._post(api_url, {"token": token, "content": "record", "action": "import",
                                 "format": "json", "type": "flat", "overwriteBehavior": "overwrite",
                                 "data": json.dumps(rows[i:i + batch]), "returnContent": "count",
                                 "returnFormat": "json"})
        if isinstance(res, dict) and "error" in res:
            raise RuntimeError(f"REDCap API error: {res['error']}")
        done += int(res.get("count", 0)) if isinstance(res, dict) else 0
    return done


def main() -> int:
    now = datetime.now(TZ)
    today = now.date()
    dry = os.environ.get("DIGEST_DRY_RUN", "") == "1"
    force = os.environ.get("DIGEST_FORCE", "") == "1"
    only = {x.strip() for x in os.environ.get("DIGEST_ONLY_RECORDS", "").split(",") if x.strip()}
    token = os.environ.get("REDCAP_STAFF_TOKEN", "").strip()
    api_url = os.environ.get("REDCAP_API_URL", "").strip() or fg.DEFAULT_API_URL

    grants = json.loads((ROOT / "data/grants.json").read_text(encoding="utf-8"))
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"sent_ids": [], "monthly": ""}
    sent_ids = set(state.get("sent_ids", []))
    kinds = ["weekly"] + (["monthly"] if state.get("monthly") != today.strftime("%Y-%m") else [])

    digests = {}
    for kind in kinds:
        d = build_digest(kind, grants, sent_ids, today)
        if d is None and force and grants:
            d = build_digest("monthly", grants, sent_ids, today)
        if d:
            digests[kind] = d
    if not digests:
        annotate("notice", f"Nothing new or closing within {CLOSING_DAYS} days: no digest this week")
        return 0
    for kind, (subject, body) in digests.items():
        print(f"\n===== {kind} digest =====\nSubject: {subject}\n\n{body}\n")
    if not token:
        annotate("warning", "REDCAP_STAFF_TOKEN is not set: digest built but not queued")
        return 0 if dry else 1

    try:
        staff = export_staff(api_url, token)
        rows, counts = [], {}
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        for kind, (subject, body) in digests.items():
            ids = [i for i in recipients(staff, kind) if not only or i in only]
            counts[kind] = len(ids)
            rows += [{"record_id": i, "digest_queued": stamp, "digest_kind": kind,
                      "digest_subject": subject, "digest_body": body,
                      "funding_digest_complete": "2"} for i in ids]
        summary = ", ".join(f"{k} {v}" for k, v in counts.items())
        if dry:
            annotate("notice", f"DRY RUN: would queue {len(rows)} digests ({summary}) from {len(staff)} staff records")
            return 0
        n = import_digests(api_url, token, rows)
    except RuntimeError as exc:
        hint = " (a 403 here usually means the server firewall rejected the text)" if "403" in str(exc) else ""
        annotate("error", f"queueing failed: {exc}{hint}")
        return 2

    if not only:  # test sends to a few records do not mark calls as announced
        sent_ids |= {g["id"] for g in grants}
        state = {"sent_ids": sorted(sent_ids, key=lambda x: (len(x), x)), "last_queued": stamp,
                 "monthly": today.strftime("%Y-%m") if "monthly" in digests else state.get("monthly", "")}
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    annotate("notice", f"Queued {n} digests ({summary}){' [test records only]' if only else ''}; "
                       "REDCap Alerts sends them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
