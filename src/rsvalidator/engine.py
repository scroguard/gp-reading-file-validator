"""Runs all validation layers on an uploaded file and groups the results into a report."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from . import mvrs
from .fields import FieldChecker
from .grouping import Grouping
from .issues import Issue, Severity
from .lines import check_framing, split_lines
from .spec import load_format

# Formats the app can validate: key -> display name. Validators are wired up in validate().
FORMATS = {
    "mvrs": "MV-RS Host Download",
    "fcs-csv": "FCS CSV Import",
    "fcs-xml": "FCS XML Import",
    "temetra-csv": "Temetra CSV Import",
    "temetra-xml": "Temetra XML Import",
}


@dataclass
class Section:
    kind: str  # "file" | "route" | "account"
    title: str
    first_line: int | None
    issues: list[Issue]
    corrections: list = field(default_factory=list)  # temetra_fix.Correction, made in the corrected file

    @property
    def errors(self) -> int:
        return sum(i.severity == Severity.ERROR for i in self.issues)

    @property
    def recommended(self) -> int:
        return sum(i.severity == Severity.RECOMMENDED for i in self.issues)

    @property
    def warnings(self) -> int:
        return sum(i.severity == Severity.WARNING for i in self.issues)

    @property
    def worst(self) -> Severity | None:
        return min((i.severity for i in self.issues), key=lambda s: s.rank, default=None)


@dataclass
class SummaryRow:
    category: str
    severity: Severity
    count: int
    first_line: int | None
    accounts: int


@dataclass
class CorrectedFile:
    filename: str
    content: bytes
    count: int  # rows corrected
    added_columns: list[str]
    errors_after: int  # must-correct findings left in the corrected file
    recommended_after: int


@dataclass
class Report:
    filename: str
    format_name: str
    guide: str
    created: datetime
    line_count: int
    route_count: int
    account_count: int
    sections: list[Section]
    summary: list[SummaryRow] = field(default_factory=list)
    corrected: "CorrectedFile | None" = None

    @property
    def errors(self) -> int:
        return sum(s.errors for s in self.sections)

    @property
    def recommended(self) -> int:
        return sum(s.recommended for s in self.sections)

    @property
    def warnings(self) -> int:
        return sum(s.warnings for s in self.sections)

    @property
    def accounts_with_errors(self) -> int:
        return sum(1 for s in self.sections if s.kind == "account" and s.errors)

    @property
    def valid(self) -> bool:
        return self.errors == 0


def validate(content: bytes, filename: str, format_key: str = "mvrs") -> Report:
    fmt = FORMATS[format_key]
    if format_key == "mvrs":
        issues, grouping, guide = _validate_mvrs(content)
    elif format_key == "temetra-csv":
        from .temetra_csv import GUIDE, validate_temetra_csv

        issues, grouping = validate_temetra_csv(content, filename)
        guide = GUIDE
    else:
        from .fcs_csv import validate_csv
        from .fcs_spec import load_xml_spec
        from .fcs_xml import validate_xml

        product = format_key.split("-")[0]
        issues, grouping = validate_csv(content) if format_key == "fcs-csv" else validate_xml(content, product)
        guide = load_xml_spec(product).guide
    if not content:
        issues = [Issue(severity=Severity.ERROR, code="empty-file", summary="Empty file",
                        message="The uploaded file is empty.")]
    report = _build_report(filename, fmt, guide, grouping, issues)
    if format_key == "temetra-csv" and any(i.code == "duplicate-cref" for i in issues):
        _attach_corrections(report, content, filename, grouping, issues)
    return report


def _attach_corrections(report: Report, content: bytes, filename: str, grouping: Grouping, issues: list[Issue]):
    """Offer a corrected file for rows that repeat a meter as a second register (Temetra CSV)."""
    from .temetra_fix import fix_register_duplicates

    fixed = fix_register_duplicates(content, filename)
    if fixed is None:
        return
    after = validate(fixed.content, fixed.filename, "temetra-csv")
    report.corrected = CorrectedFile(fixed.filename, fixed.content, len(fixed.corrections), fixed.added_columns,
                                     after.errors, after.recommended)
    by_line = {c.line: c for c in fixed.corrections}
    for issue in issues:
        c = by_line.get(issue.line) if issue.code == "duplicate-cref" else None
        if c:
            issue.fix = "Corrected automatically in the corrected file: " + "; ".join(c.changes) + "."
    sections = {("account", i): s for i, s in enumerate(s for s in report.sections if s.kind == "account")}
    for c in fixed.corrections:
        owner = grouping.owner.get(c.line, ("file", 0))
        section = sections.get(owner, report.sections[0])
        section.corrections.append(c)


def _validate_mvrs(content: bytes) -> tuple[list[Issue], Grouping, str]:
    spec = load_format("mvrs")
    lines, issues = split_lines(content)
    issues += check_framing(lines, spec.data_length, spec.line_length)

    checker = FieldChecker(spec)
    builder = mvrs.TreeBuilder(spec)
    for line in lines:
        rid = spec.record_id_of(line.data)
        rec = spec.record(rid)
        if rec is None:
            issues.append(Issue(
                severity=Severity.ERROR, code="unknown-record", summary="Unknown record type",
                line=line.number, record=line.data[:3], start=1, end=3, field="Record ID",
                message=(f"Line {line.number} starts with \"{line.data[:3]}\", which is not a record type defined "
                         f"in the {spec.format} layout. Every line must start with a valid three-character record ID."),
            ))
            builder.add_unknown(line)
            continue
        issues += checker.check_record(line.number, rid, rec, line.data)
        builder.add(line, rid, rec)
    builder.finish(lines[-1].number if lines else 0)
    issues += builder.issues
    issues += mvrs.RuleChecker(spec, builder.tree).run()

    tree = builder.tree
    route_of_account = {a.index: r.number for r in tree.routes for a in r.accounts}
    grouping = Grouping(
        line_count=len(lines),
        routes=[(f"Route {r.number or '(unknown)'}, starting on line {r.first_line}", r.first_line) for r in tree.routes],
        accounts=[(_account_title(a, route_of_account.get(a.index, "?")), a.cus.n) for a in tree.accounts],
        owner=tree.owner,
    )
    return issues, grouping, spec.guide


def _account_title(acct: mvrs.Account, route: str) -> str:
    # Names are usually "LAST, FIRST" in host files, so no possessive form.
    parts = [f"account # {acct.account_number}" if acct.account_number else "no account number", f"route {route}"]
    who = acct.name or "Unnamed customer"
    return f"{who} \u2014 {', '.join(parts)}, starting on line {acct.cus.n}"


def _build_report(filename: str, format_name: str, guide: str, grouping: Grouping, issues: list[Issue]) -> Report:
    buckets: dict[tuple[str, int], list[Issue]] = defaultdict(list)
    for issue in issues:
        key = issue.owner or (grouping.owner.get(issue.line, ("file", 0)) if issue.line else ("file", 0))
        buckets[key].append(issue)

    sections: list[Section] = [Section("file", "File, header and structure checks", None, buckets.pop(("file", 0), []))]
    for i, (title, first) in enumerate(grouping.routes):
        sections.append(Section("route", title, first, buckets.pop(("route", i), [])))
    for i, (title, first) in enumerate(grouping.accounts):
        sections.append(Section("account", title, first, buckets.pop(("account", i), [])))
    for leftover in buckets.values():  # owner index with no section; keep the issues visible
        sections[0].issues.extend(leftover)
    for s in sections:
        s.issues.sort(key=lambda i: (i.line or 0, i.start or i.column or 0, i.severity.rank))

    rows: dict[tuple[str, Severity], SummaryRow] = {}
    for s in sections:
        seen_here = set()
        for i in s.issues:
            k = (i.category, i.severity)
            row = rows.get(k)
            if row is None:
                row = rows[k] = SummaryRow(i.category, i.severity, 0, i.line, 0)
            row.count += 1
            if i.line and (row.first_line is None or i.line < row.first_line):
                row.first_line = i.line
            if s.kind == "account" and k not in seen_here:
                row.accounts += 1
                seen_here.add(k)
    summary = sorted(rows.values(), key=lambda r: (r.severity.rank, -r.count, r.category))

    return Report(
        filename=filename,
        format_name=format_name,
        guide=guide,
        created=datetime.now(),
        line_count=grouping.line_count,
        route_count=len(grouping.routes),
        account_count=len(grouping.accounts),
        sections=sections,
        summary=summary,
    )
