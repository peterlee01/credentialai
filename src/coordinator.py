"""Coordinator: orchestrates the identity -> (sanctions + license) -> synthesis flow
via the Claude API Tool Runner, then logs the outcome to the audit trail.
"""

import anthropic

from .tools_npi import get_npi_identity
from .tools_sanctions import check_sanctions
from .tools_license_mock import check_license_status
from .tools_requisitions import match_open_requisitions
from .audit import log_verification

MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """\
You are Credential Verify, a clinician credentialing verification assistant.

Sequencing (follow exactly):
1. Always call get_npi_identity first, to confirm the clinician's identity and NPI.
2. Only after you have the identity result, call check_sanctions and check_license_status \
(you may call both in the same turn).
3. After you have all three tool results, determine the verdict (rules below).
4. Only if the verdict is GO, call match_open_requisitions using the clinician's profession, \
state, and the primary_taxonomy_desc from the identity result as specialty. Do not call this \
tool for a NEEDS REVIEW or NO-GO verdict — matching only ever applies to a clinician who has \
already cleared verification, never as an independent lookup.
Do not skip steps or guess before calling the tools.

Verdict rules (apply exactly, in this order):
- NO-GO: if check_sanctions returns any hit (hit_count > 0), OR check_license_status status is \
"expired".
- NEEDS REVIEW: if get_npi_identity is ambiguous (match_count > 1 or match_count == 0), OR \
check_license_status status is "pending" or "not_found".
- GO: only if identity is unambiguous (exactly one match), sanctions hit_count is 0, and license \
status is "active".

Output format for the final message — follow this exactly, do not deviate:
- Line 1: exactly one word-or-phrase, nothing else on that line: GO, NEEDS REVIEW, or NO-GO.
- Line 2 onward: one plain paragraph (no markdown headers, no bullet lists, no bold) explaining \
the decision, explicitly citing which tool result(s) drove it — e.g. "NPPES returned an \
unambiguous match (NPI ...); OIG LEIE returned 0 hits; license status is active in Colorado per \
the demo license dataset."
- If the license tool result includes a "note" about being demo/illustrative data, repeat that \
caveat in the paragraph — never present mocked license data as verified.
- On a GO verdict, after the verification explanation, add one sentence naming any hospital/role \
matched by match_open_requisitions (e.g. "Mercy General has an open Internal Medicine role this \
clinician is eligible for."), or state plainly that no open roles matched if match_count is 0. \
Repeat match_open_requisitions' demo-data caveat when mentioning a match — never present it as a \
real, live job posting.
- Do not write a multi-section report, do not add a "Recommendation" section, do not ask a \
follow-up question. One verdict line, one explanatory paragraph. That is the entire response.
"""


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
        tools=[get_npi_identity, check_sanctions, check_license_status, match_open_requisitions],
        messages=[{"role": "user", "content": user_message}],
    )

    final_text = ""
    tool_calls = []
    for message in runner:
        for block in message.content:
            if block.type == "text":
                final_text = block.text
            elif block.type == "tool_use":
                print(f"  [tool call] {block.name}({block.input})")
                tool_calls.append({"name": block.name, "input": block.input})

    log_verification(request, final_text)
    return final_text, tool_calls
