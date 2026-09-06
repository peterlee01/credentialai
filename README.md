# Credential Verify

An agentic credentialing verification assistant: a Claude-orchestrated coordinator that checks a
clinician's identity (real NPPES NPI Registry), sanctions/exclusion status (real HHS-OIG LEIE),
and license status (mocked — see caveat below), then synthesizes a cited GO / NEEDS REVIEW /
NO-GO verdict.

A portfolio project demonstrating real multi-agent orchestration — a coordinator directing
identity, sanctions, and license sub-checks against real government data sources, with a cited,
confidence-tiered, human-escalatable trust pattern driving every verdict.

## Real vs. mocked data sources

| Source | Status |
|---|---|
| NPPES NPI Registry (identity) | **Real, live, public API** — no key required |
| HHS-OIG LEIE (sanctions/exclusions) | **Real, live, public CSV** — downloaded and cached locally |
| State license/board status | **Mocked.** No unified free public API exists across all 50 state boards. A small illustrative dataset (10 states × 4 professions) stands in so the pipeline can be exercised end-to-end. Every response from this tool is labeled as demo data — never treat it as verified current law. |
| Hospital open requisitions | **Mocked.** A small illustrative list of open roles at a handful of hospitals, used only for eligibility matching (see "Requisition matching" below) — not a live job feed from any real hospital. |

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
`psychologist`, `counselor`, `"social worker"`, `physician` (the four covered by the mocked
license dataset).

## Architecture

See the [agent architecture spec](https://claude.ai/code/artifact/9013eeae-8ff0-47d8-86a5-e6ccf84e5467)
for the full design rationale (why identity resolves first, why the synthesis step — not any
single lookup — is the actual product). In short:

```
Coordinator (Claude, via Tool Runner)
  -> get_npi_identity        (real: NPPES)
  -> check_sanctions         (real: HHS-OIG LEIE)      \  called after identity
  -> check_license_status    (mocked, clearly labeled)  /  resolves, in parallel
  -> [only if verdict is GO] match_open_requisitions (mocked, clearly labeled)
  -> synthesized verdict: GO / NEEDS REVIEW / NO-GO, cited
```

## Requisition matching

Once a clinician clears verification (GO), the coordinator also checks which
of a hospital's own already-open roles they're now eligible for. This is
**eligibility-gated matching against a single hospital's existing
requisitions — not a physician-hospital marketplace.** There's no
physician-side discovery and no cross-hospital liquidity being built here;
building an actual two-sided marketplace means solving supply/demand
liquidity from scratch, which is a much harder and different business (see
the "not yet" call on marketplace matching in the portfolio case study, and
Nomad Health's 2026 marketplace wind-down as the cautionary example). Matching
only ever runs downstream of a clean verdict — credentialing first, placement
second.

Every verification is appended to `audit_log.jsonl` (gitignored) with the request, and the full
verdict text.

**All four verdict paths verified end-to-end against real NPPES/LEIE data** (not just designed —
actually run and observed):
- `GO` — "Shoshana Aal, counselor, Colorado": a real, unambiguous single NPPES match (NPI
  1093982704), 0 sanctions hits, active mocked license
- `NEEDS REVIEW` — "Jack Smith, social worker, California": real ambiguous NPPES match (5
  candidates, no clean profession match)
- `NO-GO` via sanctions — "John Smith, social worker, California": 2 real HHS-OIG LEIE hits
- `NO-GO` via expired license — "Sarah Johnson, social worker, Virginia": mocked expired status
- `GO` with a requisition match — "David Abbey, physician, Colorado": a real, unambiguous single
  NPPES match (NPI 1568483196), 0 sanctions hits, active mocked license, matched to a mocked open
  Internal Medicine role at Mercy General
- `GO` with no requisition match — "Daniel Abelev, physician, Arizona": a real, unambiguous single
  NPPES match (NPI 1417573163), 0 sanctions hits, active mocked license, correctly reported no
  open roles matched in the mocked requisition dataset for that state

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
