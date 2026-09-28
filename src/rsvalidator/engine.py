"""Runs all validation layers on an uploaded file and groups the results into a report."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from . import mvrs
from .fields import FieldChecker
from .issues import Issue, Severity
from .lines import check_framing, split_lines
from .spec import load_format

# Formats the app can validate: key -> display name. Each key has a
# formats/<key>.yaml layout; format-specific rules are wired up in validate().
FORMATS = {"mvrs": "MV-RS Host Download"}


@dataclass
class Section:
    kind: str  # "file" | "route" | "account"
    title: str
    first_line: int | None
    issues: list[Issue]

    @property
    def errors(self) -> int:
        return sum(i.severity == Severity.ERROR for i in self.issues)

    @property
    def warnings(self) -> int:
        return sum(i.severity == Severity.WARNING for i in self.issues)


@dataclass
class SummaryRow:
    category: str
    severity: Severity
    count: int
    first_line: int | None
    accounts: int


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

    @property
    def errors(self) -> int:
        return sum(s.errors for s in self.sections)

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
    spec = load_format(format_key)
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

    if not lines:
        issues.append(Issue(severity=Severity.ERROR, code="empty-file", summary="Empty file",
                            message="The uploaded file is empty."))

    return _build_report(filename, spec, lines, builder.tree, issues)


def _account_title(acct: mvrs.Account, route: str) -> str:
    # Names are usually "LAST, FIRST" in host files, so no possessive form.
    parts = [f"account # {acct.account_number}" if acct.account_number else "no account number", f"route {route}"]
    who = acct.name or "Unnamed customer"
    return f"{who} \u2014 {', '.join(parts)}, starting on line {acct.cus.n}"


def _build_report(filename, spec, lines, tree: mvrs.FileTree, issues: list[Issue]) -> Report:
    route_of_account = {a.index: r.number for r in tree.routes for a in r.accounts}
    buckets: dict[tuple[str, int], list[Issue]] = defaultdict(list)
    for issue in issues:
        key = tree.owner.get(issue.line, ("file", 0)) if issue.line else ("file", 0)
        buckets[key].append(issue)

    sections: list[Section] = []
    file_issues = buckets.pop(("file", 0), [])
    sections.append(Section("file", "File, cycle and structure checks", None, file_issues))
    for route in tree.routes:
        sections.append(Section(
            "route", f"Route {route.number or '(unknown)'}, starting on line {route.first_line}",
            route.first_line, buckets.pop(("route", route.index), []),
        ))
    for acct in tree.accounts:
        sections.append(Section(
            "account", _account_title(acct, route_of_account.get(acct.index, "?")),
            acct.cus.n, buckets.pop(("account", acct.index), []),
        ))
    for s in sections:
        s.issues.sort(key=lambda i: (i.line or 0, i.start or 0, i.severity != Severity.ERROR))

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
    summary = sorted(rows.values(), key=lambda r: (r.severity != Severity.ERROR, -r.count, r.category))

    return Report(
        filename=filename,
        format_name=spec.format,
        guide=spec.guide,
        created=datetime.now(),
        line_count=len(lines),
        route_count=len(tree.routes),
        account_count=len(tree.accounts),
        sections=sections,
        summary=summary,
    )
