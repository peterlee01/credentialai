"""Coordinator: orchestrates the identity -> (sanctions + license) -> synthesis flow
via the Claude API Tool Runner, then logs the outcome to the audit trail.
"""

import json

import anthropic

from .tools_npi import get_npi_identity
from .tools_sanctions import check_sanctions
from .tools_license import check_license_status
from .tools_facilities import match_facilities, rank_facility_matches
from .audit import log_verification

MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """\
You are Credential Verify, a clinician credentialing verification assistant.

Sequencing (follow exactly):
1. Always call get_npi_identity first, with the clinician's profession, to confirm their identity \
and NPI. It only counts NPPES records whose taxonomy fits that profession; same-name records in \
other fields are reported in name_matches_other_professions and do not make the identity ambiguous.
2. Only after you have the identity result, call check_sanctions and check_license_status \
(you may call both in the same turn).
When calling check_license_status, pass the clinician's first and last name, and the license \
number from the identity result if it has one.
3. After you have all three tool results, determine the verdict (rules below).
4. Only if the verdict is GO, call match_facilities using the clinician's profession, state, and \
the primary_taxonomy_desc, practice_city and practice_zip from the identity result. Do not call \
this tool for a NEEDS REVIEW or NO-GO verdict — matching only ever applies to a clinician who has \
already cleared verification, never as an independent lookup.
5. After match_facilities returns, call rank_facility_matches once with up to 5 of its candidates, \
best fit first. Weigh closeness to the practice address, how well the facility type suits the \
profession and specialty, and CMS rating. Give each a one-sentence reason under 15 words that \
uses only facts from the match_facilities result. Never state or imply that a facility is hiring.
Do not skip steps or guess before calling the tools.

Verdict rules (apply exactly, in this order):
- NO-GO: if check_sanctions returns any hit (hit_count > 0), OR check_license_status status is \
"expired" or "revoked".
- NEEDS REVIEW: if get_npi_identity is ambiguous (match_count > 1 or match_count == 0), OR \
check_license_status status is "pending", "not_found", "ambiguous" or "unverified".
- GO: only if identity is unambiguous (exactly one match), sanctions hit_count is 0, and license \
status is "active".

Output format for the final message — follow this exactly, do not deviate:
- Line 1: exactly one word-or-phrase, nothing else on that line: GO, NEEDS REVIEW, or NO-GO.
- Line 2 onward: one short plain paragraph (2-4 sentences, no markdown headers, no bullet lists, no \
bold) written for a hiring manager, not an engineer. Use everyday words and say what each check \
found and where it came from, e.g. "Timothy Shea is a clinical psychologist in Denver listed in \
the national provider registry. Shea doesn't appear on the federal list of excluded providers, \
and the Colorado licensing board shows the psychologist license as active through August 2027." \
Refer to the clinician by name, never as he or she. \
Do not mention tool names, field names, JSON, match counts, status codes or NPI numbers. Dates \
are written out (August 2027, not 2027-08-31).
- If several registry records share the name, say so plainly (e.g. "two people with this name are \
listed, so a person needs to confirm which one you mean").
- If the license status is "unverified", say plainly that the license couldn't be checked \
automatically for that state and a person needs to confirm it — never imply a license was verified \
when it was not. If a license record shows discipline on record, mention it in plain words.
- If the state license number matches the one in the provider registry, you may say the license \
number matches, which confirms it's the same person.
- The paragraph should only explain the identity/sanctions/license decision. Do not mention \
match_facilities, rank_facility_matches or any facility match in the paragraph text — that is surfaced \
separately in the product UI, not in this prose.
- Do not write a multi-section report, do not add a "Recommendation" section, do not ask a \
follow-up question. One verdict line, one explanatory paragraph. That is the entire response.
"""


def _parse_result(content):
    """A tool result's JSON payload, or None if it isn't JSON."""
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    try:
        return json.loads(content) if isinstance(content, str) else None
    except ValueError:
        return None


def run_verification(first_name: str, last_name: str, state: str, profession: str):
    request = {
        "first_name": first_name,
        "last_name": last_name,
        "state": state,
        "profession": profession,
    }

    user_message = (
        f"Verify this clinician: {first_name} {last_name}, practicing in {state} as a "
        f"{profession}."
    )

    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[get_npi_identity, check_sanctions, check_license_status, match_facilities,
               rank_facility_matches],
        messages=[{"role": "user", "content": user_message}],
    )

    final_text = ""
    tool_calls = []
    for message in runner:
        calls_by_id = {}
        for block in message.content:
            if block.type == "text":
                final_text = block.text
            elif block.type == "tool_use":
                print(f"  [tool call] {block.name}({block.input})")
                call = {"name": block.name, "input": block.input, "result": None}
                tool_calls.append(call)
                calls_by_id[block.id] = call
        if calls_by_id:
            # The runner caches this response, so reading it here doesn't run the tools twice.
            try:
                response = runner.generate_tool_call_response() or {}
            except Exception:
                response = {}  # results only feed the UI summary; the run itself is unaffected
            for item in response.get("content", []):
                call = calls_by_id.get(item.get("tool_use_id"))
                if call is not None:
                    call["result"] = _parse_result(item.get("content"))

    log_verification(request, final_text)
    return final_text, tool_calls
