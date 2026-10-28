# MUHAS Office of Sponsored Programmes portal

Part of the MUHAS OSP research-funding portal. Phase 1 (this site): verified funding opportunities.
Planned phases: grant applications, awards, and funded projects, linked to the MUHAS staff directory
(PI and team by staff record) — internal data stays in REDCap; only approved public summaries are published.

A public snapshot of research funding calls for MUHAS researchers, verified by MUHAS OSP.

Live site: <https://muhas-osp.github.io/> (repository `muhas-osp/muhas-osp.github.io`)

| Path | Section | Status |
|---|---|---|
| `/` | OSP home | live |
| `/opportunities/` | Funding opportunities (`data/grants.json`) | live |
| `/projects/` | Funded projects (public fields only) | planned |
| `/awards/` | Awards summary | planned |

Each section follows the same pattern: REDCap instrument → nightly script → `data/<section>.json` → static page.

## How it works

```
REDCap (utafiti.muhas.ac.tz)  ──nightly 00:07 EAT──▶  scripts/fetch_grants.py  ──▶  data/grants.json
   grants project, OSP verifies                       (verified + eligible +              │
                                                       open rounds only)                   ▼
                                                                     opportunities/ page renders it
                                                                     → GitHub Pages (Actions deploy)
```

A call appears on the site only when, in REDCap:

* **OSP verification status** is *Verified - publish*,
* **Tanzania / MUHAS eligibility** is not *Not eligible*,
* funder, title and a direct `https://` link are filled in, and
* it is **Rolling**, or at least one deadline round is today or later.

Internal fields (`osp_notes`, `source_text`, `entry_method`, status fields) are never written to
`data/grants.json`; a test enforces this. Expired rounds also roll off in the browser between
nightly runs.

Same conventions as `muhas-dcepd.github.io`: standard-library Python, static HTML (`.nojekyll`),
an allowlist staging step (`scripts/stage_site.py`), and Pages deployed from Actions.

## Repository layout

| Path | Purpose | Published? |
|---|---|---|
| `index.html` | OSP home | yes |
| `opportunities/index.html`, `assets/osp.css`, `assets/opportunities.js` | Funding opportunities board | yes |
| `data/grants.json` | Nightly REDCap export (public fields only) | yes |
| `scripts/fetch_grants.py` | REDCap → `grants.json` (stdlib only) | no |
| `scripts/stage_site.py` | Copies the public allowlist into `_site/` | no |
| `tests/test_funding.py` | Publish rules, privacy sentinel, importer parsing | no |
| `admin/grants_data_dictionary.csv` | Upload to REDCap to create the project | no |
| `admin/import_catalogue.py` | OSP `.xlsx` catalogue → REDCap import CSV (needs `openpyxl`) | no |
| `admin/parse_grant_email.py` | Optional: LLM extraction of a call email → REDCap (Pending) | no |

## Setup

1. **REDCap**: create a project, upload `admin/grants_data_dictionary.csv`. Create an API user with
   **export rights only** and generate its token. (Imports use a separate admin token, kept off GitHub.)
2. **GitHub**: Settings → Secrets and variables → Actions → add `REDCAP_GRANTS_TOKEN`.
   Settings → Pages → Source: **GitHub Actions**.
3. Actions → *Refresh and deploy funding opportunities* → **Run workflow**.

Run locally:

```bash
export REDCAP_GRANTS_TOKEN=...        # export-only token
python scripts/fetch_grants.py        # writes data/grants.json
python -m unittest discover -s tests
python scripts/stage_site.py && python -m http.server -d _site 8000
```

## Loading OSP spreadsheets

```bash
pip install openpyxl
python admin/import_catalogue.py "Funding opportunities_Oct 2026.xlsx" -o redcap_import.csv
```

Upload `redcap_import.csv` in REDCap → Data Import Tool. Every imported call arrives as
*Pending OSP verification*; the sheet's original deadline wording is kept in `osp_notes`.
Do not commit import files to this public repository.
