"""Renders a validation Report as a PDF."""

from __future__ import annotations

from fpdf import FPDF, FontFace
from fpdf.enums import XPos, YPos

from .engine import Report
from .issues import Severity

ERROR_RGB = (180, 35, 24)
REC_RGB = (154, 74, 0)
WARN_RGB = (93, 103, 120)
COLORS = {Severity.ERROR: ERROR_RGB, Severity.RECOMMENDED: REC_RGB, Severity.WARNING: WARN_RGB}
MUTED_RGB = (93, 103, 120)
LINE_RGB = (221, 226, 234)


def _t(text: str) -> str:
    # Core PDF fonts are Latin-1; uploads are decoded as Latin-1 so this is lossless for file data.
    for fancy, plain in (("\u2014", "-"), ("\u2026", "..."), ("\u2192", "->"), ("\u2019", "'"), ("\u2018", "'"),
                         ("\u201c", '"'), ("\u201d", '"')):
        text = text.replace(fancy, plain)
    return text.encode("latin-1", "replace").decode("latin-1")


class _Doc(FPDF):
    def __init__(self, report: Report):
        super().__init__(orientation="P", unit="mm", format="Letter")
        self.report = report
        self.set_auto_page_break(auto=True, margin=15)
        self.set_margins(15, 15, 15)
        self.set_title(f"Validation report - {report.filename}")
        self.set_creator("Reading System File Validator")

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*MUTED_RGB)
        self.cell(0, 5, _t(f"{self.report.filename} - {self.report.format_name} validation report"),
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(2)
        self.set_text_color(0)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*MUTED_RGB)
        self.cell(0, 5, f"Page {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(0)


def render_pdf(report: Report, customer: str = "") -> bytes:
    pdf = _Doc(report)
    pdf.alias_nb_pages()
    pdf.add_page()
    width = pdf.epw

    kind = "Host download file report" if report.format_name.startswith("MV-RS") else f"{report.format_name} file report"
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(width, 8, _t(f"{kind} generated for {customer}" if customer else kind),
                   align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "B", 11)
    pdf.multi_cell(width, 6, _t(f"File: {report.filename}"), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*MUTED_RGB)
    pdf.multi_cell(width, 5, _t(
        f"{report.format_name} - checked {report.created:%B %d, %Y %I:%M %p} against {report.guide}"),
        align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0)
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 11)
    if report.errors:
        pdf.set_text_color(*ERROR_RGB)
        verdict = f"{report.errors:,} problem(s) must be corrected."
    elif report.recommended:
        pdf.set_text_color(*REC_RGB)
        verdict = "Nothing must be corrected."
    else:
        pdf.set_text_color(26, 127, 55)
        verdict = "Nothing must be corrected."
    if report.recommended:
        verdict += f" {report.recommended:,} correction(s) highly recommended."
    if report.warnings:
        verdict += f" {report.warnings:,} warning(s)."
    pdf.cell(0, 6, _t(verdict), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, _t(
        f"Lines: {report.line_count:,}    Routes: {report.route_count}    Accounts: {report.account_count:,}    "
        f"Accounts to correct: {report.accounts_with_errors:,}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 8.5)
    for sev, text in ((Severity.ERROR, "the import fails or the data is wrong."),
                      (Severity.RECOMMENDED, "accepted by some systems (e.g. FCS) but may cause problems in others "
                                             "(e.g. Temetra or MV-RS)."),
                      (Severity.WARNING, "worth knowing; the file imports as intended.")):
        pdf.set_text_color(*COLORS[sev])
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.cell(36, 4.6, sev.label)
        pdf.set_text_color(0)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.cell(0, 4.6, _t(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    if report.corrected:
        fx = report.corrected
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 7, "Corrections made", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)
        after = (f"The corrected file still has {fx.errors_after} problem(s) that must be corrected by hand."
                 if fx.errors_after else "The corrected file has no problems that must be corrected.")
        pdf.multi_cell(width, 4.6, _t(
            f"A corrected file ({fx.filename}) was produced. {fx.count} row(s) were second registers of a meter "
            f"(for example kW demand) that repeated the main meter's CREF and meter serial, which makes Temetra "
            f"overwrite the main meter and lose its reading. Following Itron's example, each extra register was "
            f"given a -1, -2, ... suffix on its CREF and meter serial, LINKEDMETERSERIAL set to the original meter "
            f"serial, and an original-meter-serial= tag in ADDTAG. Every other row is unchanged. Each account below "
            f"lists the exact changes. {after} Please also correct the export process so future files come out "
            f"this way."), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)

    if report.summary:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 7, "Summary", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 8.5)
        with pdf.table(
            col_widths=(84, 30, 22, 18, 20),
            text_align=("LEFT", "LEFT", "RIGHT", "RIGHT", "RIGHT"),
            line_height=4.6,
            borders_layout="HORIZONTAL_LINES",
            headings_style=FontFace(emphasis="BOLD", color=MUTED_RGB),
        ) as table:
            head = table.row()
            for h in ("Problem", "Category", "Occurrences", "Accounts", "First line"):
                head.cell(h)
            for r in report.summary:
                row = table.row()
                row.cell(_t(r.category))
                row.cell(r.severity.label)
                row.cell(f"{r.count:,}")
                row.cell(f"{r.accounts:,}" if r.accounts else "-")
                row.cell(str(r.first_line or "-"))
        pdf.ln(4)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 7, "Details", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    clean = 0
    for section in report.sections:
        if not section.issues:
            clean += section.kind == "account"
            continue
        if pdf.will_page_break(18):
            pdf.add_page()
        pdf.ln(1.5)
        pdf.set_draw_color(*LINE_RGB)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
        pdf.ln(1.5)
        pdf.set_font("Helvetica", "B", 10)
        kind = "problems" if section.errors or section.recommended else "warnings"
        pdf.multi_cell(width, 5, _t(f"{section.title} contains the following {kind}:"),
                       align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        for issue in section.issues:
            where = " / ".join(x for x in (
                f"Line {issue.line}" if issue.line else None,
                issue.position_label,
                issue.path,
                issue.record,
            ) if x)
            pdf.set_x(pdf.l_margin + 4)
            pdf.set_font("Helvetica", "B", 8.5)
            pdf.set_text_color(*COLORS[issue.severity])
            pdf.cell(18, 4.6, {Severity.ERROR: "MUST FIX", Severity.RECOMMENDED: "RECOMMEND",
                               Severity.WARNING: "WARNING"}[issue.severity])
            pdf.set_text_color(*MUTED_RGB)
            pdf.cell(0, 4.6, _t(where), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_text_color(0)
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_x(pdf.l_margin + 22)
            text = issue.message + (f" (Guide p.{issue.page})" if issue.page else "")
            pdf.multi_cell(width - 22, 4.3, _t(text), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if issue.fix:
                pdf.set_x(pdf.l_margin + 22)
                pdf.multi_cell(width - 22, 4.3, "**How to fix:** " + _t(issue.fix).replace("**", "* *"), align="L",
                               markdown=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(0.8)
        if section.corrections:
            pdf.set_x(pdf.l_margin + 4)
            pdf.set_font("Helvetica", "B", 8.5)
            pdf.set_text_color(31, 95, 191)
            pdf.cell(0, 4.6, "Corrections made in the corrected file:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_text_color(0)
            pdf.set_font("Helvetica", "", 8.5)
            for c in section.corrections:
                pdf.set_x(pdf.l_margin + 22)
                pdf.multi_cell(width - 22, 4.3, _t("- " + c.summary), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(0.8)
    if clean:
        pdf.ln(3)
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*MUTED_RGB)
        pdf.cell(0, 5, f"{clean:,} account(s) had no problems and are not listed.")
    return bytes(pdf.output())
