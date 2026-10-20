"""Downloadable PDF verification report, built on the server from the run's own results.

The report is generated when the checks run and returned with the result, so
its contents come straight from the primary-source lookups rather than from
anything the browser sends back.
"""

import uuid
from datetime import datetime, timezone

from fpdf import FPDF

_DISCLAIMER = (
    "Generated automatically from public primary sources at the time shown. Identity: NPPES NPI Registry. "
    "Sanctions: HHS-OIG List of Excluded Individuals/Entities. License: state board public license records "
    "(Colorado, Washington, Connecticut; other states are routed to a person). This report supports, and does not replace, a credentialing "
    "committee's review."
)
_TONE_RGB = {"ok": (0, 140, 110), "warn": (170, 115, 0), "bad": (200, 30, 40)}
_VERDICT_RGB = {"GO": (0, 140, 110), "NEEDS REVIEW": (170, 115, 0), "NO-GO": (200, 30, 40)}


def _latin1(text) -> str:
    """The PDF core fonts are Latin-1; swap typographic characters for plain ones."""
    text = str(text or "")
    for a, b in {"–": "-", "—": "-", "·": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "↗": "", "…": "..."}.items():
        text = text.replace(a, b)
    return text.encode("latin-1", "replace").decode("latin-1")


class _Report(FPDF):
    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", size=7.5)
        self.set_text_color(120, 120, 120)
        self.cell(0, 5, f"Credentialai verification report - page {self.page_no()}", align="C")


def build_report_pdf(first_name, last_name, state, profession, verdict_text, summary, matches=None,
                     report_id=None, generated_at=None, sample=False) -> bytes:
    report_id = report_id or uuid.uuid4().hex[:12].upper()
    generated_at = generated_at or datetime.now(timezone.utc)
    lines = (verdict_text or "").strip().split("\n")
    verdict = lines[0].strip().upper() if lines else ""
    explanation = " ".join(l.strip() for l in lines[1:]).strip()
    person = (summary or {}).get("person") or {}
    checks = (summary or {}).get("checks") or []

    pdf = _Report(format="Letter")
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 10, "Credentialai Verification Report", new_x="LMARGIN", new_y="NEXT")
    if sample:
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(200, 30, 40)
        pdf.cell(0, 6, "SAMPLE - illustrative data, not a real verification", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 5, _latin1(f"Report ID {report_id}  -  Generated {generated_at:%B %d, %Y at %H:%M} UTC"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    def section(title):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(210, 210, 210)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
        pdf.ln(2)

    def field(label, value):
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(90, 90, 90)
        pdf.cell(38, 6, label)
        pdf.set_font("Helvetica", size=10)
        pdf.set_text_color(20, 20, 20)
        pdf.multi_cell(width - 38, 6, _latin1(value), new_x="LMARGIN", new_y="NEXT")

    section("Clinician")
    field("Name", f"{first_name} {last_name}")
    field("Profession", profession.title())
    field("Specialty", person.get("specialty") or profession.title())
    field("State", state)
    field("NPI", person.get("npi") or "Not confirmed")

    section("Verdict")
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*_VERDICT_RGB.get(verdict, (20, 20, 20)))
    clear = (summary or {}).get("checks_clear")
    pdf.cell(0, 9, _latin1(verdict + (f"   ({clear} of {len(checks)} checks clear)" if checks else "")),
             new_x="LMARGIN", new_y="NEXT")
    if explanation:
        pdf.set_font("Helvetica", size=10)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 5.5, _latin1(explanation), new_x="LMARGIN", new_y="NEXT")

    section("Checks")
    for check in checks:
        pdf.set_font("Helvetica", "B", 10.5)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(width - 45, 6.5, _latin1(check.get("label")))
        pdf.set_text_color(*_TONE_RGB.get(check.get("tone"), (90, 90, 90)))
        pdf.cell(45, 6.5, _latin1(check.get("status")), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=9)
        pdf.set_text_color(90, 90, 90)
        source = check.get("source") or ""
        if check.get("source_url"):
            source += f" ({check['source_url']})"
        pdf.multi_cell(0, 5, _latin1("Source: " + source), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 5.5, _latin1(check.get("detail") or ""), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.5)

    if matches:
        section("Matched employers hiring (informational)")
        for i, m in enumerate(matches, 1):
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(20, 20, 20)
            pct = f"  -  {m['match_pct']}% match" if m.get("match_pct") is not None else ""
            pdf.multi_cell(0, 5.5, _latin1(f"{i}. {m.get('name')} ({m.get('city')}, {m.get('state')}){pct}"),
                           new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", size=9.5)
            pdf.set_text_color(60, 60, 60)
            for job in ((m.get("openings") or {}).get("listings") or []):
                pay = f" - {job['pay']}" if job.get("pay") else ""
                pdf.set_x(pdf.l_margin + 5)
                pdf.multi_cell(width - 5, 5, _latin1(f"- {job.get('title')}{pay}"), link=job.get("link") or "",
                               new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.5)

    section("About this report")
    pdf.set_font("Helvetica", size=8.5)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(0, 4.5, _latin1(_DISCLAIMER), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
