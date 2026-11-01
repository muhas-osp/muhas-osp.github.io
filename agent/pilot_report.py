#!/usr/bin/env python3
"""
pilot_report.py: short pilot report for OSP from the agent's usage log and a REDCap export.

  python agent/pilot_report.py --export GrantsProject_DATA_....csv [--since 2026-11-03] [-o pilot_report.md]

--export  REDCap export (raw codes) of the grants project, both forms, taken by OSP
          (needs osp_status, which the agent's own token cannot see).
Reports: runs (live / dry), candidates drafted, approval rate (Verified among AI drafts with an
OSP decision), needs_review rate, searches, tokens and estimated cost (agent/prices.json).
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export")
    ap.add_argument("--since", default="")
    ap.add_argument("-o", "--output", default="pilot_report.md")
    a = ap.parse_args()

    usage = [r for r in csv.DictReader(open(ROOT / "data/agent_usage.csv")) if r["time"][:10] >= a.since]
    prices = json.loads((ROOT / "agent/prices.json").read_text())
    runs = defaultdict(lambda: {"dry": "1", "drafts": 0, "searches": 0, "in": 0, "out": 0, "cw": 0, "cr": 0, "cost": 0.0, "priced": True})
    for r in usage:
        x = runs[r["run_id"]]
        x["dry"], x["drafts"] = r["dry_run"], int(r["drafts"])
        x["searches"] += int(r["web_searches"])
        for k, col in (("in", "input_tokens"), ("out", "output_tokens"), ("cw", "cache_write_tokens"), ("cr", "cache_read_tokens")):
            x[k] += int(r[col])
        p = prices["models"].get(r["model"], {})
        if any(p.get(k) is None for k in ("input", "output", "cache_write", "cache_read")):
            x["priced"] = False
        else:
            x["cost"] += (int(r["input_tokens"]) * p["input"] + int(r["output_tokens"]) * p["output"]
                          + int(r["cache_write_tokens"]) * p["cache_write"] + int(r["cache_read_tokens"]) * p["cache_read"]) / 1e6
        x["cost"] += int(r["web_searches"]) * prices["web_search_per_1000"] / 1000

    lines = ["# MUHAS OSP discovery agent: pilot report", ""]
    priced = all(x["priced"] for x in runs.values())
    total = sum(x["cost"] for x in runs.values())
    lines += [f"- Runs: {len(runs)} ({sum(x['dry'] == '0' for x in runs.values())} live, {sum(x['dry'] == '1' for x in runs.values())} dry)",
              f"- New drafts: {sum(x['drafts'] for x in runs.values())}",
              f"- Web searches: {sum(x['searches'] for x in runs.values())}",
              f"- Tokens: {sum(x['in'] for x in runs.values()):,} input, {sum(x['out'] for x in runs.values()):,} output, "
              f"{sum(x['cr'] for x in runs.values()):,} cache reads",
              f"- Estimated cost: " + (f"USD {total:.2f} (about USD {total / max(len(runs), 1):.2f} per run)" if priced
                                       else "fill agent/prices.json, or read the Claude Console usage page")]
    if a.export:
        recs = [r for r in csv.DictReader(open(a.export, encoding="utf-8-sig")) if r.get("ai_run_id") and r["ai_run_id"][:8] >= a.since.replace("-", "")]
        decided = [r for r in recs if r.get("osp_status") in ("2", "3")]
        approved = [r for r in decided if r["osp_status"] == "2"]
        review = [r for r in recs if r.get("needs_review") == "1"]
        pct = lambda n, d: f"{100 * n / d:.0f}%" if d else "n/a"
        lines += ["", "## OSP decisions on AI drafts",
                  f"- Drafts in REDCap: {len(recs)}",
                  f"- Decided by OSP: {len(decided)}; approved {len(approved)} ({pct(len(approved), len(decided))})",
                  f"- Flagged needs_review: {len(review)} ({pct(len(review), len(recs))})",
                  f"- Still waiting for OSP: {len(recs) - len(decided)}"]
    lines += ["", "## Per run", "", "| Run | Mode | Drafts | Searches | Est. cost (USD) |", "|---|---|---|---|---|"]
    for rid, x in sorted(runs.items()):
        lines.append(f"| {rid} | {'dry' if x['dry'] == '1' else 'live'} | {x['drafts']} | {x['searches']} | "
                     + (f"{x['cost']:.2f}" if x["priced"] else "n/a") + " |")
    Path(a.output).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
