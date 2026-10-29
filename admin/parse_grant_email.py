#!/usr/bin/env python3
"""
parse_grant_email.py — Extract the grant schema from a raw call-for-proposals
email with an LLM (local Ollama or OpenAI), validate it, and import it into
REDCap as *Pending OSP verification*.

fetch_grants.py publishes only records whose osp_status is "Verified - publish",
so nothing AI-extracted reaches the portal until OSP has checked the deadline,
budget, eligibility and link against the official call. Tanzania/MUHAS
eligibility is always left as "Pending assessment" — that is an OSP judgement,
not something the model should decide.

Env:
  LLM_BACKEND          "ollama" (default) or "openai"
  OLLAMA_HOST          default http://localhost:11434
  OLLAMA_MODEL         default llama3.1:8b
  OPENAI_API_KEY       required for openai backend
  OPENAI_MODEL         required for openai backend (any model with structured outputs)
  REDCAP_API_URL
  REDCAP_IMPORT_TOKEN  a *separate* token with import rights (not the CI export token)

Usage:
  python parse_grant_email.py call.txt            # extract + print
  python parse_grant_email.py call.txt --push     # extract + import to REDCap
"""
from __future__ import annotations

import re
import json
import os
import sys
from datetime import date
from urllib.parse import urlparse

import requests

# Must match grants_data_dictionary.csv (label -> code).
REGION_CODES = {"Tanzania": "1", "East Africa": "2", "Sub-Saharan Africa": "3",
                "Africa (continent-wide)": "4", "LMICs": "5", "Global": "6"}
CAREER_CODES = {"Early-career": "1", "Mid-career": "2", "Senior / established": "3", "Any stage": "4"}
CURRENCIES = ["USD", "EUR", "GBP", "TZS"]

NULL_STR = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["funder_name", "grant_title", "call_reference", "application_url",
                 "snapshot_summary", "research_area", "eligible_regions", "career_stages",
                 "eligible_applicants", "award_budget_text", "max_funding_amount",
                 "funding_currency", "award_period", "opening_date", "rolling",
                 "deadlines", "deadline_note"],
    "properties": {
        "funder_name": {"type": "string"},
        "grant_title": {"type": "string"},
        "call_reference": NULL_STR,
        "application_url": NULL_STR,
        "snapshot_summary": {"type": "string"},
        "research_area": NULL_STR,
        "eligible_regions": {"type": "array", "items": {"type": "string", "enum": list(REGION_CODES)}},
        "career_stages": {"type": "array", "items": {"type": "string", "enum": list(CAREER_CODES)}},
        "eligible_applicants": NULL_STR,
        "award_budget_text": NULL_STR,
        "max_funding_amount": {"type": ["number", "null"]},
        "funding_currency": {"type": ["string", "null"], "enum": CURRENCIES + [None]},
        "award_period": NULL_STR,
        "opening_date": NULL_STR,
        "rolling": {"type": "boolean"},
        "deadlines": {"type": "array", "items": {"type": "string"}},
        "deadline_note": NULL_STR,
    },
}

SYSTEM_PROMPT = (
    "You extract structured data from research funding announcements. Use only "
    "information stated in the text; never guess. If a value is absent or ambiguous, "
    "return null (or an empty list). "
    "call_reference: the funder's identifier (e.g. RFA-HG-27-009, PAR-24-174). "
    "application_url: the link to this specific call, not a generic funder homepage. "
    "snapshot_summary: exactly two plain sentences on what the call funds and why. "
    "research_area: short topics separated by semicolons. "
    "eligible_applicants: one or two short sentences on who can apply (degree, track "
    "record, institution type). "
    "award_budget_text: the budget as published, briefly (e.g. 'Up to $275,000 over 2 years'); "
    "max_funding_amount: the maximum per award as a plain number. "
    "rolling: true only if the call says apply anytime / rolling / no fixed deadline. "
    "deadlines: every application deadline round as YYYY-MM-DD, ascending (full-application "
    "deadlines; use letter-of-intent dates only if no other dates are given). "
    "deadline_note: time-of-day or time-zone rules for the deadline, if stated. "
    "eligible_regions and career_stages: choose only from the allowed values that the "
    "eligibility criteria clearly cover. Return JSON only."
)


def _call_ollama(text: str) -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    r = requests.post(f"{host}/api/chat", timeout=300, json={
        "model": os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
        "stream": False,
        "format": SCHEMA,                       # Ollama structured outputs
        "options": {"temperature": 0},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": text}],
    })
    r.raise_for_status()
    return r.json()["message"]["content"]


def _call_openai(text: str) -> str:
    r = requests.post("https://api.openai.com/v1/chat/completions", timeout=120,
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={
            "model": os.environ["OPENAI_MODEL"],
            "temperature": 0,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "grant_call", "strict": True, "schema": SCHEMA}},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": text}],
        })
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _iso(s) -> date | None:
    try:
        return date.fromisoformat(str(s or ""))
    except ValueError:
        return None


def extract_grant_fields(email_text: str, backend: str | None = None) -> dict:
    """Return validated fields plus a `_warnings` list for the OSP reviewer."""
    backend = (backend or os.environ.get("LLM_BACKEND", "ollama")).lower()
    data = json.loads(_call_ollama(email_text) if backend == "ollama" else _call_openai(email_text))
    warnings: list[str] = []

    # Deterministic checks the model can't be trusted with.
    good = sorted({d for s in data.get("deadlines") or [] if (d := _iso(s))})
    bad = [s for s in data.get("deadlines") or [] if not _iso(s)]
    if bad:
        warnings.append(f"unparseable deadline(s) dropped: {bad}")
    if good and good[-1] < date.today():
        warnings.append("all deadline rounds are in the past")
    if not good and not data.get("rolling"):
        warnings.append("no deadline found and not marked rolling")
    if len(good) > 6:
        warnings.append(f"{len(good) - 6} round(s) beyond 6 not imported")
    data["deadlines"] = [d.isoformat() for d in good]
    if data.get("opening_date") and not _iso(data["opening_date"]):
        warnings.append(f"bad opening date {data['opening_date']!r} dropped")
        data["opening_date"] = None

    url = str(data.get("application_url") or "")
    p = urlparse(url)
    if not (p.scheme in ("http", "https") and p.netloc):
        warnings.append(f"no valid application URL: {url!r}")
        data["application_url"] = None
    elif url not in email_text:
        warnings.append("application URL not found verbatim in source — check it")

    data["eligible_regions"] = [r for r in data.get("eligible_regions") or [] if r in REGION_CODES]
    data["career_stages"] = [c for c in data.get("career_stages") or [] if c in CAREER_CODES]
    data["_warnings"] = warnings
    return data


def firewall_safe(value: str) -> str:
    """The MUHAS server firewall blocks REDCap form saves containing ';'. Use ',' instead."""
    return re.sub(r"\s*;\s*", ", ", value)


def push_to_redcap(fields: dict, source_text: str) -> str:
    """Import one record as Pending OSP verification; returns the new record ID."""
    s = lambda k: "" if fields.get(k) is None else str(fields[k])
    rec = {
        "record_id": "new",                          # replaced by forceAutoNumber
        **{k: s(k) for k in ("funder_name", "grant_title", "call_reference", "application_url",
                             "snapshot_summary", "research_area", "eligible_applicants",
                             "award_budget_text", "max_funding_amount", "funding_currency",
                             "award_period", "opening_date", "deadline_note")},
        "tz_eligibility": "3",                       # Pending assessment — OSP decides
        "deadline_type": "2" if fields.get("rolling") and not fields.get("deadlines") else "1",
        "osp_status": "1",                           # Pending OSP verification
        "osp_notes": "AI-extracted. " + " ".join(fields.get("_warnings", [])),
        "entry_method": "3",                         # AI-parsed
        "source_text": source_text[:60000],
    }
    for i, d in enumerate(fields.get("deadlines", [])[:6]):
        rec["deadline_date" if i == 0 else f"deadline_date_{i + 1}"] = d
    for label, code in REGION_CODES.items():
        rec[f"eligible_regions___{code}"] = "1" if label in fields.get("eligible_regions", []) else "0"
    for label, code in CAREER_CODES.items():
        rec[f"career_stage___{code}"] = "1" if label in fields.get("career_stages", []) else "0"
    rec = {k: v if k == "application_url" else firewall_safe(v) for k, v in rec.items()}

    r = requests.post(os.environ["REDCAP_API_URL"], timeout=60, data={
        "token": os.environ["REDCAP_IMPORT_TOKEN"],
        "content": "record", "action": "import", "format": "json", "type": "flat",
        "overwriteBehavior": "normal", "forceAutoNumber": "true",
        "returnContent": "ids", "returnFormat": "json",
        "data": json.dumps([rec]),
    })
    if r.status_code != 200:
        raise RuntimeError(f"REDCap import failed ({r.status_code}): {r.text[:300]}")
    return r.json()[0]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    text = open(sys.argv[1], encoding="utf-8").read()
    fields = extract_grant_fields(text)
    print(json.dumps(fields, indent=2, ensure_ascii=False))
    if "--push" in sys.argv:
        print("Imported as record", push_to_redcap(fields, text), "(Pending OSP verification)")
