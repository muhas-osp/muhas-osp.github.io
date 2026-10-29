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

## Weekly funding digest (email)

Every Monday at 06:37 EAT, `.github/workflows/weekly-digest.yml` runs `scripts/queue_digest.py`:

* **Weekly** (default preference): calls new since the last digest, plus calls closing within 30 days.
  Not sent in a week with neither.
* **Monthly** (staff who chose it): all open calls, on the first run of each month.
* **Who**: staff directory categories 1–3 (academic, adjunct/honorary, emeritus), not retired/left,
  funding alerts on, not unsubscribed.

The script writes the digest into the `funding_digest` form of each recipient's staff record;
a REDCap Alert sends it from utafiti.muhas.ac.tz. The script never reads emails or names
(it exports only record_id, category, employment_status, pref_funding, alert_frequency,
unsubscribed_all), and only call IDs are kept in `data/digest_state.json`.

### One-time setup (staff directory project)

1. Download the current data dictionary, then
   `python admin/append_dictionary.py <downloaded>.csv admin/staff_digest_fields.csv -o with_digest.csv`
   and upload `with_digest.csv` (adds the `funding_digest` form).
2. Make `staff_preferences` a survey (Online Designer → Enable as survey), so the email can carry a
   preferences / unsubscribe link.
3. Alerts & Notifications → Add alert:
   * Trigger: *When conditional logic is TRUE during a form/survey save or data import*, logic
     `[digest_queued] <> "" and datediff([digest_queued], "now", "m") < 60 and [email] <> ""`
   * Send immediately; allow it to be sent **more than once per record**; tick *ensure logic is still true*.
   * To: `[email]`. Subject: `[digest_subject]`. Body:
     `Dear colleague,` / `[digest_body]` / `Update your interests or unsubscribe: [survey-link:staff_preferences:your preferences]`
4. API token for `REDCAP_STAFF_TOKEN` (repository secret): API export + API import, view/edit
   on `funding_digest`. Store it only as a GitHub secret.
5. Test: Actions → *Weekly funding digest* → Run workflow with `dry_run` off and `only_records` = your
   own record_id. Check the email, then let Monday's run go to everyone.

If REDCap mail cannot be used, the fallback is sending from ospmuhas@gmail.com (Gmail allows
about 500 recipients a day, close to the current list size).
