"""Temetra CSV import validation (Temetra NAM CSV File Format Guide LDI-0665).

Temetra reads columns by heading name, not position, except that a Network New
Asset file must start with IGNORE, CANCREATE. The file type is recognised from
the file name and the headings, and each type has its own mandatory columns.
"""

from __future__ import annotations

import csv
import difflib
import io
import re
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path

import yaml

from .fcs_csv import decode
from .grouping import Grouping
from .issues import Issue, Severity

FORMATS_DIR = Path(__file__).parent / "formats"
YES_NO = {"yes", "no", "true", "false"}
TRUE_FALSE = {"true", "false"}
EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
DATETIME = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?)?$")
# GPS column: hemisphere letters with decimal degrees or degrees + decimal minutes (p.30-31),
# e.g. "N26.672412 W81.773412" or "N47 38.650140 W117 4.707960".
GPS = re.compile(r"^([NS])\s?(\d{1,2}(?:\.(\d+)|\s\d{1,2}\.(\d+)))[\s,]+([EW])\s?(\d{1,3}(?:\.(\d+)|\s\d{1,2}\.(\d+)))$")


@cache
def load_rules() -> dict:
    return yaml.safe_load((FORMATS_DIR / "temetra.yaml").read_text())["csv"]


GUIDE = "Itron Temetra NAM CSV File Format Guide LDI-0665 REV 002 (December 2024)"


@dataclass
class Col:
    index: int
    raw: str
    name: str
    rule: dict | None


def _issue(line, code, summary, message, severity=Severity.ERROR, column=None, field=None, page=None, owner=None):
    return Issue(severity=severity, code=code, summary=summary, message=message, line=line, column=column,
                 field=field, page=page, owner=owner)


def detect_type(rules: dict, names: list[str], filename: str) -> str:
    base = Path(filename or "").name.lower()
    types = rules["file_types"]
    for key, t in types.items():
        if t.get("filename_prefix") and base.startswith(t["filename_prefix"]):
            return key
    head = set(names)
    for key in ("meter_replacement", "schedule", "historical_reads"):
        if set(types[key]["detect"]) <= head:
            return key
    if head & set(types["deschedule"]["detect_any"]):
        return "deschedule"
    if names[:1] == ["IGNORE"] or "CANCREATE" in head:
        return "new_asset"
    return "data_update"


def validate_temetra_csv(content: bytes, filename: str = "") -> tuple[list[Issue], Grouping]:
    rules = load_rules()
    text, issues = decode(content)
    grouping = Grouping()
    if text.lstrip().startswith("<"):
        issues.append(_issue(None, "wrong-format", "File is not a CSV file",
                             "This file looks like XML, not CSV. Choose the Temetra XML Import format instead."))
        return issues, grouping

    rows: list[tuple[int, list[str]]] = []
    reader = csv.reader(io.StringIO(text, newline=""))
    line = 1
    try:
        for values in reader:
            rows.append((line, values))
            line = reader.line_num + 1
    except csv.Error as e:
        issues.append(_issue(reader.line_num, "csv-syntax", "CSV syntax error",
                             f"Line {reader.line_num} cannot be read as CSV: {e}. Check for unbalanced double quotes."))
    grouping.line_count = max(line - 1, 0)
    rows = [(n, v) for n, v in rows if any(x.strip() for x in v)]
    if not rows:
        issues.append(_issue(None, "empty-file", "Empty file", "The file contains no header row or data."))
        return issues, grouping

    (hline, header), data = rows[0], rows[1:]
    columns = _header(rules, hline, header, issues)
    names = [c.name for c in columns]
    ftype = detect_type(rules, names, filename)
    tdef = rules["file_types"][ftype]
    _file_type_checks(rules, ftype, tdef, hline, names, filename, issues)
    by = {c.name: c for c in columns}

    unique_seen: dict[tuple[str, str], int] = {}
    crefs: dict[str, list[tuple[int, str]]] = {}  # CREF -> [(line, METERSERIAL)]
    groups: list[tuple[str, str, str, int]] = []  # (route, account, name, first line)
    routes: dict[str, int] = {}
    for n, values in data:
        get = lambda name: values[by[name].index - 1].strip() if name in by and by[name].index <= len(values) else ""
        if "IGNORE" in by and get("IGNORE").lower() in ("yes", "true"):
            continue  # rows marked IGNORE are not processed (p.10, p.20)
        if len(values) != len(columns):
            trailing = len(values) == len(columns) + 1 and values[-1] == ""
            issues.append(_issue(n, "column-count", "Wrong number of columns",
                                 f"The row on line {n} has {len(values)} columns, but the header row has {len(columns)}"
                                 + ("; the row ends with an extra comma" if trailing else
                                    ". A value containing a comma must be enclosed in double quotes")
                                 + ". Values may be read under the wrong heading.", page=7))
        account, name_, route = get("ACCOUNTREF"), get("ACCOUNTNAME"), get("ROUTENAME")
        if route not in routes:
            routes[route] = len(grouping.routes)
            grouping.routes.append((f"Route {route}" if route else "No route name", n))
        if not groups or not account or groups[-1][1] != account or groups[-1][0] != route:
            key = account or get("METERSERIAL") or get("CREF")
            groups.append((route, account, name_ or key, n))
            label = (f"{name_ or 'Unnamed customer'} — account # {account}" if account
                     else f"Meter {get('METERSERIAL') or '(no serial)'}" + (f" (CREF {get('CREF')})" if get("CREF") else ""))
            grouping.accounts.append((f"{label}{', route ' + route if route else ''}, starting on line {n}", n))
        grouping.owner[n] = ("account", len(grouping.accounts) - 1)

        for col in columns:
            if col.rule is None or col.index > len(values):
                continue
            issues += _check_value(rules, n, col, values[col.index - 1], unique_seen)
        issues += _row_rules(n, get, by, ftype)
        issues += _duplicate_cref(n, get, by, crefs)
    return issues, grouping


def _header(rules, hline, header, issues) -> list[Col]:
    fields = rules["fields"]
    cols, seen = [], {}
    for i, raw in enumerate(header, 1):
        name = raw.strip()
        col = Col(i, raw, name, None)
        cols.append(col)
        if not name:
            issues.append(_issue(hline, "header-blank", "Blank column heading",
                                 f"Column {i} of the header row is blank. Every column needs a heading.", column=i, page=7))
            continue
        if raw != name:
            issues.append(_issue(hline, "header-spaces", "Heading has extra spaces",
                                 f"Column {i} heading \"{raw}\" has leading or trailing spaces. Temetra matches headings "
                                 f"by name, so it may not recognise it as {name}.", Severity.WARNING, column=i, page=7))
        if name != name.upper():
            issues.append(_issue(hline, "header-case", "Heading not uppercase",
                                 f"Column {i} heading \"{name}\" must be all uppercase (p.7).", column=i, page=7))
            name = col.name = name.upper()
        if name in seen:
            issues.append(_issue(hline, "header-duplicate", "Duplicate heading",
                                 f"Column {i} ({name}) repeats column {seen[name]}. Headings must be unique or data may "
                                 f"not import as expected (p.7).", column=i, page=7))
        seen.setdefault(name, i)
        col.rule = fields.get(name)
        if col.rule is None:
            close = difflib.get_close_matches(name, list(fields), n=1, cutoff=0.85)
            hint = f" Did you mean {close[0]}?" if close else ""
            # Temetra imports files with unrecognised columns and ignores them (confirmed with a
            # customer file), so the column's data is silently not loaded: a warning, not an error.
            issues.append(_issue(hline, "header-unknown", "Unknown column heading",
                                 f"Column {i} ({name}) is not a field in the Temetra CSV guide's list of available "
                                 f"fields (p.15-32), so Temetra ignores it and its data is not loaded.{hint}",
                                 Severity.WARNING, column=i, page=15))
    return cols


def _file_type_checks(rules, ftype, tdef, hline, names, filename, issues):
    page = tdef.get("page")
    kind = tdef["name"]
    base = Path(filename or "").name.lower()
    prefix = tdef.get("filename_prefix")
    if prefix and not base.startswith(prefix):
        issues.append(_issue(hline, "filename", f"{kind} file name",
                             f"This looks like a {kind} file, whose file name must start with \"{prefix}\" "
                             f"(p.{page}). Temetra uses the name to recognise the file type.", page=page))
    if prefix and not base.endswith(".csv"):
        issues.append(_issue(hline, "filename", f"{kind} file name",
                             f"A {kind} file must be saved with a .csv extension (p.{page}).", page=page))
    first = tdef.get("first_columns")
    if first and names[:len(first)] != first:
        issues.append(_issue(hline, "first-columns", f"{', '.join(first)} must be the first columns",
                             f"A {kind} file must begin with the columns {', '.join(first)}, in that order (p.10, p.13), "
                             f"but it begins with {', '.join(names[:len(first)]) or 'nothing'}.", column=1, page=10))
    for req in tdef.get("required", []):
        if req not in names:
            issues.append(_issue(hline, "header-missing", "Mandatory column missing",
                                 f"A {kind} file must include the {req} column (p.{page}).", page=page))
    for group in tdef.get("required_one_of", []):
        if not any(g in names for g in group):
            issues.append(_issue(hline, "header-missing", "Mandatory column missing",
                                 f"A {kind} file must include one of {' or '.join(group)} (p.{page}).", page=page))
    for rec in tdef.get("recommended", []):
        if rec not in names:
            issues.append(_issue(hline, "header-recommended", "Column marked Required is missing",
                                 f"The guide's field list marks {rec} as Required (p.15-32), but this {kind} file has no "
                                 f"{rec} column. New meters may be created without it.", Severity.WARNING, page=15))
    if ftype == "deschedule":
        if not {"CREF", "METERSERIAL", "MREF"} & set(names):
            issues.append(_issue(hline, "header-missing", "No column to find the meter",
                                 "A De-scheduling file needs at least one of CREF, METERSERIAL or MREF (p.43).", page=43))
        if not ({"DESCHEDULE", "SCHEDULENAME"} <= set(names) or "DESCHEDULEALLFUTURE" in names):
            issues.append(_issue(hline, "header-missing", "No de-schedule columns",
                                 "A De-scheduling file needs DESCHEDULE with SCHEDULENAME, or DESCHEDULEALLFUTURE (p.43).",
                                 page=43))
    if "GPS" in names and ({"LAT", "LON"} & set(names)):
        issues.append(_issue(hline, "gps-and-latlon", "Both GPS and LAT/LON columns",
                             "Supply coordinates with either the GPS column or the LAT/LON columns, not both (p.30).",
                             Severity.WARNING, page=30))


def _check_value(rules, n, col: Col, raw: str, unique_seen) -> list[Issue]:
    rule = rules["fields"][col.rule["same_as"]] if "same_as" in col.rule else col.rule
    page = col.rule.get("page")
    value = raw.strip()

    def err(code, summary, reason, severity=Severity.ERROR):
        return [_issue(n, code, summary, f"The row on line {n} has a problem in column {col.index} ({col.name}). {reason}",
                       severity, column=col.index, field=col.name, page=page)]

    if not value:
        if rule.get("required"):
            return err("required-blank", f"{col.name} is blank", f"{col.name} cannot be blank.")
        if rule.get("not_null_warn"):
            return err("blank-value", f"{col.name} is blank",
                       f"The guide marks {col.name} as Required or not null, but it is blank.", Severity.WARNING)
        return []
    out: list[Issue] = []
    kind = rule["kind"]
    if rule.get("exact") and raw != value:
        out += err("padded-value", f"{col.name}: padded with spaces",
                   f"\"{raw}\" has leading or trailing spaces. The guide says {col.name} must exactly match an entry "
                   f"in Temetra's list, so the spaces may stop it matching.", Severity.WARNING)
    if rule.get("max") and len(value) > rule["max"]:
        out += err("length", f"{col.name}: too long", f"It can be at most {rule['max']} characters, but it is {len(value)}.")
    if rule.get("min") and len(value) < rule["min"]:
        out += err("length", f"{col.name}: too short", f"It must be at least {rule['min']} characters, but \"{value}\" "
                                                          f"is {len(value)}.")
    if kind == "yesno" and value.lower() not in YES_NO:
        out += err("invalid-value", f"{col.name}: not yes/no", f"It must be yes or no, but it is \"{value}\".")
    elif kind == "truefalse" and value.lower() not in TRUE_FALSE:
        out += err("invalid-value", f"{col.name}: not True/False", f"It must be True or False, but it is \"{value}\".")
    elif kind == "number" and not value.isdigit():
        out += err("not-number", f"{col.name}: not a number", f"It must contain digits only, but it is \"{value}\".")
    elif kind == "integer" and (not re.fullmatch(r"-?\d+", value) or len(value.lstrip("-")) > rule.get("max_digits", 99)):
        out += err("not-integer", f"{col.name}: not a whole number",
                   f"It must be a whole number of at most {rule.get('max_digits')} digits, but it is \"{value}\".")
    elif kind == "decimal" and not re.fullmatch(r"-?\d+(\.\d+)?", value):
        out += err("not-number", f"{col.name}: not a number", f"It must be a number, but it is \"{value}\".")
    elif kind == "date":
        out += _date(err, value, col.name, time_ok=False)
    elif kind == "datetime":
        out += _date(err, value, col.name, time_ok=True)
    elif kind == "email" and not EMAIL.match(value):
        out += err("invalid-email", f"{col.name}: not an email address", f"It must be a valid email address, but it is "
                                                                          f"\"{value}\".")
    elif kind == "gps":
        out += _gps(err, value)
    elif kind in ("lat", "lon"):
        out += _latlon(err, value, kind)
    elif kind == "meterformat":
        if not re.fullmatch(r"\d{1,2}(\.\d)?", value) or not any(float(value) == float(v) and value.split(".")[0] == v.split(".")[0]
                                                               for v in rule["values"]):
            out += err("invalid-value", f"{col.name}: invalid value",
                       f"It must be one of the formats in the guide ({', '.join(rule['values'])}), but it is \"{value}\".")
    elif kind == "tags":
        out += _tags(rules, n, col, value, rule, page)

    if "values" in rule and kind not in ("meterformat",) and value not in rule["values"]:
        out += err("invalid-value", f"{col.name}: invalid value",
                   f"It must be an exact match of one of: {', '.join(rule['values'])}. The value is \"{value}\".")
    if "values_warn" in rule:
        v = value.strip('"').strip() if rule.get("strip_quotes") else value
        if v not in rule["values_warn"]:
            out += err("value-not-listed", f"{col.name}: value not in the guide's list",
                       f"\"{value}\" is not in the guide's list ({', '.join(rule['values_warn'])}). Temetra's list depends "
                       f"on the network and meter type, so check it exists in Temetra.", Severity.WARNING)
    if rule.get("unique"):
        key = (col.name, value)
        if key in unique_seen:
            hint = ("" if col.name != "METERSERIAL" else
                    " If this row is a second register of the same meter, see the duplicate CREF correction for "
                    "this line.")
            # Temetra imported a customer file with duplicates without errors, so this is a warning.
            out += err("duplicate-value", f"{col.name} not unique",
                       f"\"{value}\" is also used on line {unique_seen[key]}. The guide says {col.name} must be "
                       f"unique.{hint}", Severity.WARNING)
        unique_seen.setdefault(key, n)
    return out


def _date(err, value, name, time_ok):
    m = (DATETIME if time_ok else DATE).match(value)
    shown = "DD/MM/YYYY" + (" HH:MM" if time_ok else "")
    if not m:
        return err("invalid-date", f"{name}: invalid date", f"It must be a date in {shown} format, but it is \"{value}\".")
    try:
        datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return err("invalid-date", f"{name}: invalid date",
                   f"\"{value}\" is not a real date in {shown} format (day first, then month).")
    return []


def _gps(err, value):
    m = GPS.match(value)
    if not m:
        return err("invalid-gps", "GPS: unrecognised format",
                   f"It must be in a supported format such as \"N26.672412 W81.773412\" (decimal degrees) or "
                   f"\"N47 38.650140 W117 4.707960\" (degrees and minutes) (p.30-31). The value is \"{value}\".")
    decimals = [len(d) for d in (m.group(3), m.group(4), m.group(7), m.group(8)) if d]
    if any(d < 6 for d in decimals):
        return err("gps-precision", "GPS: fewer than 6 decimal places",
                   f"The guide asks for 6 to 9 digits after the decimal point (p.30), but \"{value}\" has "
                   f"{min(decimals)}.", Severity.WARNING)
    return []


def _latlon(err, value, kind):
    limit = 90 if kind == "lat" else 180
    if not re.fullmatch(r"-?\d{1,3}\.\d+", value) or abs(float(value)) > limit:
        return err("invalid-gps", f"{kind.upper()}: invalid", f"It must be decimal degrees between -{limit} and {limit} "
                                                              f"(p.30), but it is \"{value}\".")
    decimals = len(value.split(".")[1])
    if not 6 <= decimals <= 9:
        return err("gps-precision", f"{kind.upper()}: decimal places", f"The guide asks for 6 to 9 digits after the "
                                                                     f"decimal point (p.30), but \"{value}\" has {decimals}.",
                   Severity.WARNING)
    return []


def _tags(rules, n, col, value, rule, page):
    known = rules["tags"][rule["tag_set"]]
    out = []

    def err(code, summary, reason, severity=Severity.ERROR, tag_page=None):
        out.append(_issue(n, code, summary,
                          f"The row on line {n} has a problem in column {col.index} ({col.name}). {reason}",
                          severity, column=col.index, field=col.name, page=tag_page or page))

    for tag in value.split():
        key, _, val = tag.partition("=")
        spec = known.get(key)
        if spec is None:
            err("tag-unknown", f"{col.name}: tag not in the guide",
                f"\"{key}\" is not one of the tags listed in the guide. Utilities can define their own tags, so check "
                f"the spelling.", Severity.WARNING)
            continue
        tp = spec.get("page")
        if spec.get("bare"):
            if val:
                err("tag-value", f"{col.name}: tag takes no value", f"The {key} tag does not take a value.", tag_page=tp)
            continue
        if not val:
            err("tag-value", f"{col.name}: tag needs a value", f"The {key} tag needs a value, written {key}=value.",
                tag_page=tp)
        elif "values" in spec and val not in spec["values"]:
            err("tag-value", f"{col.name}: invalid {key} value",
                f"{key} must be one of {', '.join(spec['values'])}, but it is \"{val}\".", tag_page=tp)
        elif "pattern" in spec and not re.fullmatch(spec["pattern"], val):
            err("tag-value", f"{col.name}: invalid {key} value", f"{key} must be {spec['desc']}, but it is \"{val}\".",
                tag_page=tp)
        elif "range" in spec and not (val.isdigit() and spec["range"][0] <= int(val) <= spec["range"][1]):
            err("tag-value", f"{col.name}: invalid {key} value",
                f"{key} must be a number from {spec['range'][0]} to {spec['range'][1]:,}, but it is \"{val}\".", tag_page=tp)
        elif "max" in spec and len(val) > spec["max"]:
            err("tag-value", f"{col.name}: {key} too long", f"{key} can be at most {spec['max']} characters.", tag_page=tp)
    return out


def _duplicate_cref(n, get, by, crefs) -> list[Issue]:
    """A repeated CREF overwrites the earlier meter on import (p.8), so a second register is lost.

    Confirmed with a customer file: Temetra imported it without errors, but only the last row
    (the kW register) survived for each repeated CREF.
    """
    cref, serial = get("CREF"), get("METERSERIAL")
    if not cref or "CREF" not in by:
        return []
    earlier = crefs.setdefault(cref, [])
    earlier.append((n, serial))
    if len(earlier) == 1:
        return []
    k = len(earlier) - 1  # 1 for the first repeat, 2 for the next...
    first_line, main_serial = earlier[0]
    main_serial = main_serial or serial
    linked = main_serial if k == 1 else f"{main_serial}-{k - 1}"
    fix = (f"If this is a secondary reading for a meter (for example kW demand), append a suffix to the CREF so it is "
           f"unique, as Itron's example does: CREF {cref}-{k}. Give the register its own meter serial as well "
           f"(METERSERIAL {main_serial}-{k}), set LINKEDMETERSERIAL to {linked}, and add "
           f"original-meter-serial={main_serial} to ADDTAG.")
    return [Issue(
        severity=Severity.ERROR, code="duplicate-cref", summary="Duplicate CREF (earlier meter is overwritten)",
        line=n, column=by["CREF"].index, field="CREF", page=8, fix=fix,
        message=(f"Duplicate CREF detected on line {n}: CREF {cref} is also used on line {first_line}. Temetra "
                 f"imports the file without an error, but a row whose CREF already exists overwrites that meter "
                 f"(p.8), so only the last of these rows is kept and the other reading is lost."))]


def _row_rules(n, get, by, ftype) -> list[Issue]:
    out = []
    method = get("COLLECTIONMETHOD")
    if "MIUSERIAL" in by and method and method not in ("Manual Read",) and not get("MIUSERIAL"):
        out.append(_issue(n, "miu-missing", "MIUSERIAL blank for an endpoint read",
                          f"The row on line {n} has COLLECTIONMETHOD \"{method}\" but no MIUSERIAL. Meters read by an "
                          f"endpoint need its ID in MIUSERIAL (p.17).", column=by["MIUSERIAL"].index, field="MIUSERIAL",
                          page=17))
    if get("METERTYPE").lower() == "generic" and get("METERMODEL") and get("METERMODEL").upper() not in ("GAS", "WATER", "ELECTRICITY"):
        out.append(_issue(n, "generic-model", "METERMODEL for a Generic meter",
                          f"The row on line {n} has METERTYPE Generic, so METERMODEL must be GAS, WATER or ELECTRICITY "
                          f"(p.18), but it is \"{get('METERMODEL')}\".", column=by["METERMODEL"].index, field="METERMODEL",
                          page=18))
    if ftype == "new_asset" and "CREF" in by and not get("CREF"):
        out.append(_issue(n, "cref-blank", "CREF is blank",
                          f"The row on line {n} has no CREF. The guide marks CREF as mandatory: it is the key Temetra "
                          f"uses to find the meter on later updates and replacements (p.8, p.12).",
                          column=by["CREF"].index, field="CREF", page=12))
    return out
