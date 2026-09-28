"""FCS CSV import file validation (guide chapter 1, printed pages 1-4).

The header row names each column Table.Element. Values are checked against the
same element dictionary the XML import uses. Each data row is one reading: one
Customer, Meter and MeterSessionInput (p.2).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .fcs_spec import Element, FcsSpec, check_value, load_fcs
from .grouping import Grouping
from .issues import Issue, Severity


@dataclass
class Column:
    index: int  # 1-based
    header: str
    table: str | None = None
    element: Element | None = None
    instance: int | None = None  # AdvancedAMRRequest.<n>.Element
    export_only: bool = False


@dataclass
class Row:
    line: int
    values: list[str]


@dataclass
class _Group:
    route: str
    account: str
    first_line: int
    name: str = ""
    rows: list[Row] = field(default_factory=list)


def decode(content: bytes) -> tuple[str, list[Issue]]:
    """UTF-8 or Unicode (UTF-16, with or without byte-order mark) per p.2."""
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16"), []
    if len(content) > 1 and content[1:2] == b"\x00":
        return content.decode("utf-16-le"), []
    try:
        return content.decode("utf-8-sig"), []
    except UnicodeDecodeError as e:
        return content.decode("utf-8-sig", errors="replace"), [Issue(
            severity=Severity.ERROR, code="encoding", summary="File is not UTF-8 or Unicode",
            message=(f"The file is not valid UTF-8 (first bad byte at offset {e.start}). FCS reads CSV files as UTF-8 "
                     "or Unicode (UTF-16), set by the Import: CSV Encoding parameter (p.2). Save the file with one "
                     "of those encodings."), page=2)]


def validate_csv(content: bytes) -> tuple[list[Issue], Grouping]:
    spec = load_fcs()
    rules = spec.rules["csv"]
    text, issues = decode(content)
    grouping = Grouping()

    if text.lstrip().startswith("<"):
        issues.append(Issue(severity=Severity.ERROR, code="wrong-format", summary="File is not a CSV file",
                            message="This file looks like XML, not CSV. Choose the FCS XML Import format instead."))
        return issues, grouping

    rows: list[Row] = []
    reader = csv.reader(io.StringIO(text, newline=""))
    line = 1
    try:
        for values in reader:
            rows.append(Row(line, values))
            line = reader.line_num + 1
    except csv.Error as e:
        issues.append(Issue(severity=Severity.ERROR, code="csv-syntax", summary="CSV syntax error", line=reader.line_num,
                            message=f"Line {reader.line_num} cannot be read as CSV: {e}. Check for unbalanced double quotes (p.1)."))
    grouping.line_count = max(line - 1, 0)
    # Blank lines (including a final one) are not records.
    rows = [r for r in rows if any(v.strip() for v in r.values)]
    if not rows:
        issues.append(Issue(severity=Severity.ERROR, code="empty-file", summary="Empty file",
                            message="The file contains no header row or data."))
        return issues, grouping

    header, data = rows[0], rows[1:]
    columns, header_issues = _parse_header(spec, header)
    issues += header_issues
    if not data:
        issues.append(Issue(severity=Severity.ERROR, code="no-data", summary="No data rows", line=header.line,
                            message="The file has a header row but no data rows."))

    by_name = {c.header: c for c in columns}
    groups = _group_rows(data, by_name)
    route_index: dict[str, int] = {}
    for g in groups:
        if g.route not in route_index:
            route_index[g.route] = len(grouping.routes)
            grouping.routes.append((f"Route {g.route or '(blank)'}, starting on line {g.first_line}", g.first_line))
        idx = len(grouping.accounts)
        who = g.name or "Unnamed customer"
        acct = f"account # {g.account}" if g.account else "no account number"
        grouping.accounts.append((f"{who} — {acct}, route {g.route or '(blank)'}, starting on line {g.first_line}",
                                  g.first_line))
        for r in g.rows:
            grouping.owner[r.line] = ("account", idx)

    first_worksets: dict[str, Row] = {}
    for row in data:
        route = _value(row, by_name.get("WorkSet.WorkSetID"))
        route_owner = ("route", route_index.get(route, 0))
        if len(row.values) != len(columns):
            trailing = len(row.values) == len(columns) + 1 and row.values[-1] == ""
            issues.append(Issue(
                severity=Severity.ERROR, code="column-count", summary="Wrong number of columns", line=row.line,
                message=(f"The row on line {row.line} has {len(row.values)} columns, but the header row defines "
                         f"{len(columns)}. Every row must have the same number of columns as the header"
                         + ("; this row ends with a comma, which adds an empty extra column" if trailing else
                            ". A value containing a comma must be enclosed in double quotes")
                         + " (p.1). The values in this row may be in the wrong columns."), page=1))
        for col in columns:
            if col.element is None or col.index > len(row.values):
                continue
            value = row.values[col.index - 1]
            if not value.strip():
                continue  # blank = not supplied; FCS uses the default (p.1, p.3)
            owner = route_owner if col.table == "WorkSet" else None
            if col.element.import_rule == "not-used":
                issues.append(_col_issue(row, col, "not-used", f"{col.header} is not used for import",
                                         f"The guide marks {col.header} as not used for import, so this value is ignored.",
                                         Severity.WARNING, owner))
                continue
            for p in check_value(col.element, value, csv=True):
                if col.header == "WorkSet.WorkSetID" and p.code == "length" and len(value.strip()) < 8:
                    continue  # CSV route IDs shorter than 8 are padded with leading zeros (p.2)
                issues.append(_col_issue(row, col, p.code, p.summary, p.reason, p.severity, owner))
            if col.header == "WorkSet.WorkSetID":
                issues += [_col_issue(row, col, c, s, r, sev, owner) for c, s, r, sev in _workset_id(spec, value)]
        issues += _read_method(spec, row, by_name)
        meter_col = by_name.get("Meter.MeterNumber")
        if meter_col and meter_col.index <= len(row.values) and not _value(row, meter_col):
            issues.append(_col_issue(row, meter_col, "required-blank", "Meter.MeterNumber is blank",
                                     "The meter number cannot be blank (p.67).", page_override=67))
        # WorkSet columns describe the route; FCS creates one route per WorkSetID.
        if route in first_worksets:
            first = first_worksets[route]
            for col in columns:
                if col.table == "WorkSet" and col.index <= min(len(row.values), len(first.values)):
                    a, b = first.values[col.index - 1], row.values[col.index - 1]
                    if a != b:
                        issues.append(_col_issue(
                            row, col, "workset-differs", f"{col.header} differs within the route",
                            f"It is \"{b}\" here but \"{a}\" on line {first.line}, the route's first row. FCS creates one "
                            f"route per WorkSet.WorkSetID, so only one of these values can be used.",
                            Severity.WARNING, route_owner))
        elif route or "WorkSet.WorkSetID" in by_name:
            first_worksets[route] = row
    return issues, grouping


def _parse_header(spec: FcsSpec, header: Row) -> tuple[list[Column], list[Issue]]:
    rules = spec.rules["csv"]
    issues: list[Issue] = []
    columns: list[Column] = []
    seen: dict[str, int] = {}

    def err(code, summary, message, col=None, severity=Severity.ERROR, page=1):
        issues.append(Issue(severity=severity, code=code, summary=summary, line=header.line, column=col,
                            message=message, page=page))

    for i, raw in enumerate(header.values, 1):
        name = raw.strip()
        col = Column(i, name)
        columns.append(col)
        if not name:
            err("header-blank", "Blank column name",
                f"Column {i} of the header row is blank" + (" (the header row ends with a comma; do not put a comma "
                                                            "after the last property)" if i == len(header.values) else "")
                + ". Every column needs a Table.Element name (p.1).", i)
            continue
        if name in seen:
            err("header-duplicate", "Duplicate column", f"Column {i} ({name}) repeats column {seen[name]}.", i)
        seen.setdefault(name, i)
        parts = name.split(".")
        table = parts[0]
        col.table = table
        if table in rules["not_available"]:
            err("header-unavailable", "Table not available for CSV import",
                f"Column {i} ({name}) uses the {table} table, which is available for XML import but cannot be "
                f"included in a CSV import file (p.4).", i, page=4)
            continue
        if table not in rules["tables"]:
            err("header-unknown", "Unknown column",
                f"Column {i} ({name}) is not a known FCS import property. Columns are named Table.Element, for "
                f"example WorkSet.WorkSetID (p.1).", i)
            continue
        if len(parts) == 3 and parts[1] == "CustomDataField" and table in rules["custom_data_parents"]:
            col.element = spec.entities["CustomDataField"].element(parts[2])
        elif len(parts) == 3 and table in rules["repeatable"] and parts[1].isdigit():
            col.instance = int(parts[1])
            col.element = spec.entities[table].element(parts[2])
        elif len(parts) == 2:
            col.element = spec.entities[table].element(parts[1])
        if col.element is None and parts[-1] in spec.entities[table].export_only:
            col.export_only = True
            err("header-export-only", "Export-only column",
                f"Column {i} ({name}) is an export element: FCS sets it itself, so its values in an import file are "
                f"ignored. It can be removed.", i, Severity.WARNING)
        elif col.element is None:
            err("header-unknown", "Unknown column",
                f"Column {i} ({name}) is not an element of the {table} table in the guide. Check the spelling "
                f"and capitalization (p.1).", i)

    if columns and columns[0].header != rules["first_column"]:
        err("header-first-column", "First column must be WorkSet.WorkSetID",
            f"The first column of the header row must be {rules['first_column']}, but it is "
            f"\"{columns[0].header}\". Otherwise FCS logs a message and the import fails (p.1).", 1)
    for required in rules["required_columns"]:
        if required not in seen:
            err("header-missing", "Required column missing",
                f"The header row must include {required} (p.2).", page=2)
    return columns, issues


def _value(row: Row, col: Column | None) -> str:
    if col is None or col.index > len(row.values):
        return ""
    return row.values[col.index - 1].strip()


def _group_rows(data: list[Row], by_name: dict[str, Column]) -> list[_Group]:
    """Consecutive rows for the same route and account form one account."""
    groups: list[_Group] = []
    for row in data:
        route = _value(row, by_name.get("WorkSet.WorkSetID"))
        account = _value(row, by_name.get("Customer.AccountNumber"))
        name = _value(row, by_name.get("Customer.FullName"))
        last = groups[-1] if groups else None
        if last and last.route == route and account and last.account == account:
            last.rows.append(row)
        else:
            groups.append(_Group(route, account, row.line, name, [row]))
    return groups


def _col_issue(row: Row, col: Column, code, summary, reason, severity=Severity.ERROR, owner=None,
               page_override=None) -> Issue:
    page = page_override or (col.element.page if col.element else None)
    return Issue(
        severity=severity, code=code, summary=summary, line=row.line, column=col.index, field=col.header,
        record=col.table, page=page, owner=owner,
        message=f"The row on line {row.line} has a problem in column {col.index} ({col.header}). {reason}",
    )


def _workset_id(spec: FcsSpec, value: str):
    rule = spec.rules["workset_id"]
    bad = sorted(set(value) & set(rule["forbidden"]))
    if bad:
        shown = ", ".join("space" if c == " " else f"'{c}'" for c in bad)
        yield ("route-id-characters", "WorkSet.WorkSetID: characters not allowed",
               f"A route ID cannot contain a space or any of \\ / : * ? \" < > | & ' [ = (p.54), but it contains {shown}.",
               Severity.ERROR)
    if any(c in value for c in "_%"):
        yield ("route-id-underscore", "WorkSet.WorkSetID contains _ or %",
               "Itron recommends not using _ or % in a route ID: manual loads from the handheld then require all 8 "
               "characters (p.54).", Severity.WARNING)
    if value != value.upper():
        yield ("route-id-lowercase", "WorkSet.WorkSetID has lowercase letters",
               "Lowercase letters are converted to uppercase on import (p.54).", Severity.WARNING)


def _read_method(spec: FcsSpec, row: Row, by_name: dict[str, Column]) -> list[Issue]:
    """ReadMethod must match the input table whose columns carry data (p.3)."""
    method_col = by_name.get("MeterSessionInput.ReadMethod")
    method = _value(row, method_col) or "0"
    children = spec.rules["read_method_children"]
    with_data = {c.table for c in by_name.values()
                 if c.table in set(children.values()) and _value(row, c)}
    issues = []
    prompt_col = by_name.get("MeterSessionInput.PromptCode")
    if method == "5" and _value(row, prompt_col) != "1":
        issues.append(Issue(
            severity=Severity.ERROR, code="read-method-5-prompt", summary="ReadMethod 5 without PromptCode 1",
            line=row.line, column=method_col.index if method_col else None, field="MeterSessionInput.ReadMethod",
            record="MeterSessionInput", page=78,
            message=(f"The row on line {row.line} has ReadMethod 5 (No Read), which is only valid with PromptCode 1, "
                     f"but the PromptCode is \"{_value(row, prompt_col) or 'blank'}\" (p.78).")))
    wanted = children.get(method)
    if wanted and wanted not in with_data:
        issues.append(Issue(
            severity=Severity.ERROR, code="read-method-missing-data", summary=f"ReadMethod {method} without {wanted} data",
            line=row.line, column=method_col.index if method_col else None, field="MeterSessionInput.ReadMethod",
            record="MeterSessionInput", page=3,
            message=(f"The row on line {row.line} has ReadMethod {method}, which needs {wanted} columns with data, but "
                     f"this row has none. FCS changes the ReadMethod to 0 (manual) on import (p.3).")))
    for table in sorted(with_data - {wanted}):
        codes = "/".join(k for k, v in children.items() if v == table)
        issues.append(Issue(
            severity=Severity.ERROR, code="read-method-unexpected-data", summary=f"{table} data but ReadMethod is not {codes}",
            line=row.line, column=method_col.index if method_col else None, field="MeterSessionInput.ReadMethod",
            record="MeterSessionInput", page=3,
            message=(f"The row on line {row.line} has {table} data, but its ReadMethod is {method}. {table} data is "
                     f"only saved when the ReadMethod is {codes}; otherwise it is discarded (p.3).")))
    return issues
