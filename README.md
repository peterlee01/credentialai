# Credential Verify

An agentic credentialing verification assistant: a Claude-orchestrated coordinator that checks a
clinician's identity (real NPPES NPI Registry), sanctions/exclusion status (real HHS-OIG LEIE),
and license status (mocked — see caveat below), then synthesizes a cited GO / NEEDS REVIEW /
NO-GO verdict.

Built as a portfolio companion piece to Clearway, applying the same trust pattern (cited,
confidence-tiered, human-escalatable) to a real multi-agent orchestration problem instead of a
simulated one.

## Real vs. mocked data sources

| Source | Status |
|---|---|
| NPPES NPI Registry (identity) | **Real, live, public API** — no key required |
| HHS-OIG LEIE (sanctions/exclusions) | **Real, live, public CSV** — downloaded and cached locally |
| State license/board status | **Mocked.** No unified free public API exists across all 50 state boards. A small illustrative dataset (10 states × 3 professions) stands in so the pipeline can be exercised end-to-end. Every response from this tool is labeled as demo data — never treat it as verified current law. |

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
`psychologist`, `counselor`, `"social worker"` (the only three covered by the mocked license
dataset).

## Architecture

See the [agent architecture spec](https://claude.ai/code/artifact/9013eeae-8ff0-47d8-86a5-e6ccf84e5467)
for the full design rationale (why identity resolves first, why the synthesis step — not any
single lookup — is the actual product). In short:

```
Coordinator (Claude, via Tool Runner)
  -> get_npi_identity        (real: NPPES)
  -> check_sanctions         (real: HHS-OIG LEIE)      \  called after identity
  -> check_license_status    (mocked, clearly labeled)  /  resolves, in parallel
  -> synthesized verdict: GO / NEEDS REVIEW / NO-GO, cited
```

Every verification is appended to `audit_log.jsonl` (gitignored) with the request, and the full
verdict text.

**All three verdict paths verified end-to-end against real NPPES/LEIE data** (not just designed —
actually run and observed):
- `GO` — not yet exercised against a real clean unambiguous match (still open)
- `NEEDS REVIEW` — "Jack Smith, social worker, California": real ambiguous NPPES match (5
  candidates, no clean profession match)
- `NO-GO` via sanctions — "John Smith, social worker, California": 2 real HHS-OIG LEIE hits
- `NO-GO` via expired license — "Sarah Johnson, social worker, Virginia": mocked expired status

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

## What's out of scope for this build

- No real backend/API service, and no web UI for the review queue — both are CLI tools.
- No payer enrollment, PSV document handling, or credentialing-file generation — this checks
  identity/sanctions/license status only.
- The `GO` verdict path hasn't been exercised against a real unambiguous clean match yet.
