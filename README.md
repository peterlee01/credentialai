# Credential Verify

An agentic credentialing verification assistant: a Claude-orchestrated coordinator that checks a
clinician's identity (real NPPES NPI Registry), sanctions/exclusion status (real HHS-OIG LEIE),
and license status (real Colorado DORA license records), then synthesizes a cited GO / NEEDS REVIEW /
NO-GO verdict.

A portfolio project demonstrating real multi-agent orchestration — a coordinator directing
identity, sanctions, and license sub-checks against real government data sources, with a cited,
confidence-tiered, human-escalatable trust pattern driving every verdict.

## Data sources

| Source | Status |
|---|---|
| NPPES NPI Registry (identity) | **Real, live, public API** — no key required |
| HHS-OIG LEIE (sanctions/exclusions) | **Real, live, public CSV** — downloaded and cached locally |
| Colorado DORA license records (license status) | **Real, live, public API** ([data.colorado.gov `7s5z-vewr`](https://data.colorado.gov/resource/7s5z-vewr.json)) — no key required. Colorado only: no free public API covers all 50 state boards, so any other state returns `unverified` and the verdict is NEEDS REVIEW for a person to confirm. No license status is ever invented. |
| CMS Hospital General Information (hospital details) | **Real, live, public API** — no key required. Hospital type and rating for matched hospitals. |
| Google Jobs via SerpApi (open roles) | **Real, live** — needs `SERPAPI_KEY` (free tier: 100 searches/month). Drives matching; every match has job links. |

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then add your ANTHROPIC_API_KEY
```

## Run

```bash
python main.py "Jane" "Doe" "Colorado" "psychologist"
```

Try a real, findable name/state combination for a live NPPES/LEIE result, or an unmatched
combination to see a NEEDS REVIEW / ambiguous-identity path. Supported `profession` values:
`psychologist`, `counselor`, `"social worker"`, `physician`. License status is checked live only
for Colorado.

## Architecture

See the [agent architecture spec](https://claude.ai/code/artifact/9013eeae-8ff0-47d8-86a5-e6ccf84e5467)
for the full design rationale (why identity resolves first, why the synthesis step — not any
single lookup — is the actual product). In short:

```
Coordinator (Claude, via Tool Runner)
  -> get_npi_identity        (real: NPPES)
  -> check_sanctions         (real: HHS-OIG LEIE)      \  called after identity
  -> check_license_status    (real: Colorado DORA)     /  resolves, in parallel
  -> [only if verdict is GO] match_facilities        (real: CMS hospital list)
  -> [only if verdict is GO] rank_facility_matches   (coordinator orders the shortlist, with reasons)
  -> synthesized verdict: GO / NEEDS REVIEW / NO-GO, cited
```

## Matching (employers that are hiring)

Once a clinician clears verification (GO), the coordinator matches them to
employers near their practice address that have open roles in their
profession, so every match comes with job links. `match_facilities`:

1. Runs Google Jobs searches through SerpApi (`SERPAPI_KEY`) for the profession
   in the practice city and the cities of the best-fitting CMS hospitals, plus
   searches aimed at those hospitals directly.
2. Keeps listings whose title is in the profession (support roles such as
   assistants and technicians are dropped) and drops staffing agencies.
3. Groups listings by employer. A listing counts toward a CMS hospital when the
   employer is that hospital, or its owning health system with the listing in
   the hospital's city (Colorado systems for now).
4. Scores each employer on closeness to the practice address, how well the
   setting suits the profession, CMS rating for hospitals, and number of open
   roles.

The coordinator then orders the shortlist and writes a one-sentence reason for
each via `rank_facility_matches`; if it supplies no valid ranking, the scored
order is used. Without job data (no key, or every search failed) matching falls
back to the CMS hospital shortlist, which has no job links. Searches are cached
for a day per warm instance; a run uses roughly 3–6 searches. Matching only ever
runs downstream of a clean verdict — credentialing first, matching second.

Every verification is appended to `audit_log.jsonl` (gitignored) with the request, and the full
verdict text.

**Verdict paths verified end-to-end against real NPPES/LEIE data** (run before license status
was switched to live Colorado records — re-run these against the live license source):
- `GO` — "Shoshana Aal, counselor, Colorado": a real, unambiguous single NPPES match (NPI
  1093982704), 0 sanctions hits
- `NEEDS REVIEW` — "Jack Smith, social worker, California": real ambiguous NPPES match (5
  candidates, no clean profession match)
- `NO-GO` via sanctions — "John Smith, social worker, California": 2 real HHS-OIG LEIE hits
- `NO-GO` via expired or revoked license — a Colorado clinician whose DORA record is expired,
  revoked or suspended

## Verification report (PDF)

Every live run returns a downloadable PDF built on the server by `src/report.py`
(fpdf2) from the run's own results: clinician (name, profession, specialty,
state, NPI), the verdict and explanation, each check with its source and the
record found (NPI, exclusion entries, license number, status, expiry and
discipline), and the matched employers with their open roles and pay. Each
report has an ID and a UTC timestamp. Because the PDF is generated server-side
at verification time, its contents come from the primary-source lookups, not
from anything the browser sends back. Preview mode downloads
`sample-report.pdf`, a clearly marked sample.

## Human review queue

NEEDS REVIEW verdicts aren't just logged and abandoned — `review.py` is a real, working
human-in-the-loop tool:

```bash
python review.py
```

It scans `audit_log.jsonl` for unresolved NEEDS REVIEW cases, lets you pick one, shows the full
cited verdict, and requires a decision (approve/deny) **and** a rationale before it'll let you
resolve it. Resolutions are appended to `resolutions.jsonl` (gitignored) and the case drops out of
the queue — this closes the loop from the architecture spec's "human escalation" design goal with
actual functioning code, not a UI mockup.

## Web deployment

`api/verify.py` (a Vercel Python Function) plus the root-level `index.html` /
`about.html` / `contact.html` / `privacy.html` / `terms.html` are the deployed
web layer for the live demo at credentialai.co. It's the same `src/` package
the CLI uses — `api/verify.py` calls `run_verification` directly, so the CLI
and the live product no longer drift apart. The one difference: a deployed
function's source bundle is read-only, so the audit log there writes to
`/tmp` (ephemeral, per-instance) via the `AUDIT_LOG_PATH` env var, rather than
to the repo-relative `audit_log.jsonl` the CLI uses as its real, durable
audit trail.

## What's out of scope for this build

- No web UI for the review queue — `review.py` is still a CLI-only tool.
- No payer enrollment, PSV document handling, or credentialing-file generation — this checks
  identity/sanctions/license status only.
