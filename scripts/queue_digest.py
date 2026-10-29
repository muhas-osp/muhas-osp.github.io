#!/usr/bin/env python3
"""
queue_digest.py: weekly funding digest for MUHAS staff, emailed from the OSP Gmail account.

Runs daily (GitHub Actions). On Mondays it builds the digest from data/grants.json and
queues the eligible staff (by record_id). Every run sends to queued staff, at most
DIGEST_DAILY_CAP (499) emails per day, one email per person; the rest wait for the next day.

Who: staff directory categories 1-3 (academic, adjunct/honorary, emeritus), not retired/left,
funding alerts on, not unsubscribed, with an email.
  weekly (default)  new calls since the last digest + calls closing within 30 days;
                    nothing is sent in a week with neither
  monthly           staff who chose monthly: all open calls, first Monday of the month

Privacy: emails are read from REDCap at send time and kept in memory only. Logs and
data/digest_state.json contain counts and record_ids, never addresses or names.

Environment:
  REDCAP_STAFF_TOKEN   staff-project token (export: record_id, email, category, status, preferences)
  GMAIL_USER           default ospmuhas@gmail.com
  GMAIL_APP_PASSWORD   Google app password for that account
  DIGEST_DAILY_CAP     default 499
  DIGEST_DRY_RUN       "1": build and count, send nothing, change no state
  DIGEST_ONLY_RECORDS  comma-separated record_ids: send this week's digest to them only (testing)
  DIGEST_TEST_TO       an address: send one sample to it only (testing)
  DIGEST_FORCE         "1": build a new digest now even if it is not Monday or nothing is new
Exit codes: 0 ok, 1 configuration error, 2 REDCap/mail error.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fetch_grants", ROOT / "scripts/fetch_grants.py")
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)

TZ = ZoneInfo("Africa/Dar_es_Salaam")
BOARD_URL = "https://muhas-osp.github.io/opportunities/"
CLOSING_DAYS = 30
STAFF_FIELDS = ["record_id", "email", "category", "employment_status", "pref_funding",
                "alert_frequency", "unsubscribed_all"]
ELIGIBLE_CATEGORIES = {"1", "2", "3"}
STATE_FILE = ROOT / "data/digest_state.json"
EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[a-z]{2,}$", re.I)


def annotate(level: str, message: str) -> None:
    print(message, flush=True)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title=Funding digest::{message}", flush=True)


def safe(text: str) -> str:
    text = re.sub(r"\s*;\s*", ", ", str(text or ""))
    return " ".join(text.replace("<", "(").replace(">", ")").split())


def fmt_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%-d %b %Y")


# --------------------------------------------------------------------------- #
# Digest content (pure, tested)
# --------------------------------------------------------------------------- #
def classify(grants, sent_ids, today):
    soon = today + timedelta(days=CLOSING_DAYS)
    new = [g for g in grants if g["id"] not in sent_ids]
    closing = [g for g in grants if g.get("next_deadline")
               and date.fromisoformat(g["next_deadline"]) <= soon and g["id"] in sent_ids]
    return new, closing


def entry(g, today):
    lines = [f"* {safe(g['funder_name'])}: {safe(g['grant_title'])}"]
    if g.get("next_deadline"):
        days = (date.fromisoformat(g["next_deadline"]) - today).days
        extra = [f"Deadline {fmt_date(g['next_deadline'])} ({days} days left)"]
    else:
        extra = ["Rolling, apply any time"]
    if g.get("award_budget"):
        extra.append(f"Award: {safe(g['award_budget'])}")
    if g.get("tz_eligibility") and g["tz_eligibility"] != "Eligible":
        extra.append(f"Tanzania eligibility: {safe(g['tz_eligibility'])}")
    lines.append("  " + " | ".join(extra))
    if g.get("snapshot_summary"):
        lines.append("  " + safe(g["snapshot_summary"])[:300])
    lines.append("  " + g["application_url"])
    return "\n".join(lines)


def build_digest(kind, grants, sent_ids, today):
    """(subject, body) or None when a weekly digest has nothing to say."""
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
        parts = ([f"{len(new)} new"] if new else []) + ([f"{len(closing)} closing soon"] if closing else [])
        subject = f"MUHAS funding opportunities: {', '.join(parts)} (week of {today.strftime('%-d %b %Y')})"
    body += [f"All {len(grants)} open calls, with filters: {BOARD_URL}",
             "Questions or help with an application: Office of Sponsored Programmes, ospmuhas@gmail.com"]
    return safe(subject), "\n".join(body)


def recipients(staff, kind):
    """record_ids for this kind of digest, academic/active first (who goes first under the cap)."""
    out = []
    for r in staff:
        if (r.get("category") not in ELIGIBLE_CATEGORIES or r.get("employment_status") == "4"
                or r.get("pref_funding") == "0" or r.get("unsubscribed_all") == "1"
                or not EMAIL_RE.match((r.get("email") or "").strip())):
            continue
        if (kind == "monthly") == (r.get("alert_frequency") == "3"):
            out.append(r)
    out.sort(key=lambda r: (r.get("category") != "1", r.get("employment_status") != "1",
                            int(r["record_id"]) if str(r["record_id"]).isdigit() else 0))
    return [r["record_id"] for r in out]


def compose(sender, to, subject, body, prefs_link):
    msg = EmailMessage()
    msg["From"] = formataddr(("MUHAS Office of Sponsored Programmes", sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="muhas-osp.github.io")
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    footer = (f"Update your research interests or unsubscribe: {prefs_link}" if prefs_link
              else f"To unsubscribe, reply with the word unsubscribe.")
    msg.set_content(f"Dear colleague,\n\n{body}\n\n{footer}\n")
    return msg


def take_batch(pending, sent_today, cap):
    room = max(0, cap - sent_today)
    return pending[:room], pending[room:]


# --------------------------------------------------------------------------- #
# REDCap + Gmail
# --------------------------------------------------------------------------- #
def export_staff(api_url, token):
    body = {"token": token, "content": "record", "action": "export", "format": "json",
            "type": "flat", "rawOrLabel": "raw", "returnFormat": "json"}
    body.update({f"fields[{i}]": f for i, f in enumerate(STAFF_FIELDS)})
    data = fg._post(api_url, body)
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"REDCap API error: {data['error']}")
    return data


def survey_link(api_url, token, record_id):
    """Personal link to the staff_preferences survey, or '' if unavailable."""
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen
    try:
        req = Request(api_url, data=urlencode({"token": token, "content": "surveyLink",
                                               "format": "json", "instrument": "staff_preferences",
                                               "record": record_id, "returnFormat": "json"}).encode(),
                      headers={"User-Agent": "muhas-funding-board/1.0"})
        with urlopen(req, timeout=60) as resp:
            link = resp.read().decode().strip()
        return link if link.startswith("https://") else ""
    except Exception:
        return ""


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"sent_ids": [], "monthly": "", "outbox": {}, "pending": [], "sent_on": {}}


def main() -> int:
    now = datetime.now(TZ)
    today = now.date()
    env = os.environ.get
    dry, force = env("DIGEST_DRY_RUN", "") == "1", env("DIGEST_FORCE", "") == "1"
    only = [x.strip() for x in env("DIGEST_ONLY_RECORDS", "").split(",") if x.strip()]
    test_to = env("DIGEST_TEST_TO", "").strip()
    cap = int(env("DIGEST_DAILY_CAP", "") or 499)
    token = env("REDCAP_STAFF_TOKEN", "").strip()
    api_url = env("REDCAP_API_URL", "").strip() or fg.DEFAULT_API_URL
    sender = env("GMAIL_USER", "").strip() or "ospmuhas@gmail.com"
    password = env("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

    grants = json.loads((ROOT / "data/grants.json").read_text(encoding="utf-8"))
    state = load_state()
    sent_ids = set(state.get("sent_ids", []))
    testing = bool(only or test_to)

    # 1. Build: Mondays (or forced / testing) create this week's digests
    digests = state.get("outbox", {})
    building = testing or force or (today.weekday() == 0 and state.get("built") != today.isoformat())
    if building:
        kinds = ["weekly"] + (["monthly"] if state.get("monthly") != today.strftime("%Y-%m") else [])
        digests = {}
        for kind in kinds:
            d = build_digest(kind, grants, sent_ids, today)
            if d is None and (force or testing) and grants:
                d = build_digest("monthly", grants, sent_ids, today)
            if d:
                digests[kind] = d
        if not digests:
            annotate("notice", f"Nothing new or closing within {CLOSING_DAYS} days: no digest this week")
            if not dry and not testing:
                state["built"] = today.isoformat()
                STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
            return 0
        for kind, (subject, body) in digests.items():
            print(f"\n===== {kind} digest =====\nSubject: {subject}\n\n{body}\n")

    if test_to:
        if dry:
            annotate("notice", f"DRY RUN: would send a sample to the test address")
            return 0
        if not password:
            annotate("error", "GMAIL_APP_PASSWORD secret is not set"); return 1
        subject, body = next(iter(digests.values()))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
            smtp.login(sender, password)
            smtp.send_message(compose(sender, test_to, "[TEST] " + subject, body, ""))
        annotate("notice", "Sample digest sent to the test address")
        return 0

    if not token:
        annotate("warning", "REDCAP_STAFF_TOKEN is not set: nothing sent"); return 0 if dry else 1
    try:
        staff = export_staff(api_url, token)
    except RuntimeError as exc:
        annotate("error", f"REDCap export failed: {exc}"); return 2
    by_id = {r["record_id"]: r for r in staff}

    pending = [] if testing else list(state.get("pending", []))
    if building:
        new_pending = [[rid, kind] for kind in digests for rid in recipients(staff, kind)]
        if only:
            new_pending = [p for p in new_pending if p[0] in only] or [[rid, next(iter(digests))] for rid in only]
        if pending and not testing:
            annotate("warning", f"{len(pending)} unsent from the previous digest were replaced by this week's")
        pending = new_pending

    sent_today = state.get("sent_on", {}).get(today.isoformat(), 0)
    batch, rest = take_batch(pending, sent_today, cap)
    if dry:
        annotate("notice", f"DRY RUN: {len(pending)} queued, would send {len(batch)} today "
                           f"(cap {cap}, already sent today {sent_today}), {len(rest)} would wait")
        return 0
    if batch and not password:
        annotate("error", "GMAIL_APP_PASSWORD secret is not set"); return 1

    sent, failed, unsent = 0, 0, []
    if batch:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
            smtp.login(sender, password)
            for i, (rid, kind) in enumerate(batch):
                r = by_id.get(rid)
                email = (r or {}).get("email", "").strip()
                if not r or not EMAIL_RE.match(email) or r.get("unsubscribed_all") == "1" or kind not in digests:
                    continue                      # left, unsubscribed or email removed since queueing
                subject, body = digests[kind]
                try:
                    smtp.send_message(compose(sender, email, subject, body, survey_link(api_url, token, rid)))
                    sent += 1
                except smtplib.SMTPRecipientsRefused:
                    failed += 1                   # bad address: logged as a count only
                except (smtplib.SMTPDataError, smtplib.SMTPSenderRefused) as exc:
                    if exc.smtp_code in (421, 450, 451, 550, 552, 554) and "limit" in str(exc).lower():
                        unsent = batch[i:]        # Gmail daily limit reached: resume tomorrow
                        break
                    failed += 1
                time.sleep(0.5)

    if not testing:
        state["pending"] = unsent + rest
        state["outbox"] = digests if state["pending"] else {}
        state["sent_on"] = {today.isoformat(): sent_today + sent}
        if building:
            state["built"] = today.isoformat()
            state["sent_ids"] = sorted(sent_ids | {g["id"] for g in grants}, key=lambda x: (len(x), x))
            if "monthly" in digests:
                state["monthly"] = today.strftime("%Y-%m")
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    annotate("notice", f"Sent {sent}, failed {failed}, waiting for tomorrow {len(unsent) + len(rest)}"
                       + (" [test records only]" if only else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
