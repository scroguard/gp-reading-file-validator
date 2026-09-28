"""Per-field checks: one line, one record layout, no knowledge of other records."""

from __future__ import annotations

import re
from datetime import date

from .issues import Issue, Severity
from .spec import TYPE_LABELS, FieldSpec, FormatSpec, RecordSpec

ROUTE_NUMBER_FORBIDDEN = set(".*? ")
RF_FREQUENCY = re.compile(r"^\d+(\.\d+)?$")


def _show(value: str) -> str:
    return '"' + value.rstrip() + '"' if value.strip() else "blanks"


def _chars(chars: set[str]) -> str:
    shown = ["space" if c == " " else f"'{c}'" for c in sorted(chars)]
    return ", ".join(shown)


class FieldChecker:
    def __init__(self, spec: FormatSpec):
        self.spec = spec

    def check_record(self, line_no: int, record_id: str, rec: RecordSpec, data: str) -> list[Issue]:
        issues: list[Issue] = []
        for f in rec.fields:
            value = f.slice(data)
            if len(value) < f.length:
                # Short line; the line-length check already reports it.
                value = value.ljust(f.length)
            issues.extend(self.check_field(line_no, record_id, f, value, data))
        return issues

    def check_field(self, line_no: int, record_id: str, f: FieldSpec, value: str, data: str) -> list[Issue]:
        def issue(code: str, summary: str, reason: str, severity: Severity = Severity.ERROR) -> Issue:
            return Issue(
                severity=severity,
                code=code,
                summary=summary,
                line=line_no,
                record=record_id,
                start=f.start,
                end=f.end,
                field=f.label,
                page=f.page,
                message=(
                    f"The {record_id} record on line {line_no} contains invalid information in "
                    f"{f.bytes_label()}. These bytes are reserved for the {f.label}. {reason}"
                ),
            )

        blank = value.strip() == ""

        if f.reserved:
            return []
        if f.pad:
            if not blank:
                # MV-RS and FCS ignore pad bytes, so this is only a warning.
                return [issue("pad-not-blank", f"{f.label} not blank",
                              f"Pad bytes should be blank, but this record contains {_show(value)}. "
                              "MV-RS and FCS ignore pad bytes, so this is a warning.", Severity.WARNING)]
            return []

        if blank:
            if f.values is not None and "" in f.values:
                return []
            if f.check == "rf_frequency":
                return self._rf_frequency_blank(issue, data, value)
            if f.is_required(record_id, self.spec.trailer_ids):
                return [issue("required-blank", f"Required {f.label} is blank",
                              "This field is required, but it is blank.")]
            return []

        if f.type == "N":
            return self._numeric(issue, f, value)
        if f.type == "D":
            return self._date(issue, value)
        if f.type == "T":
            return self._time(issue, value)
        return self._text(issue, f, value)

    # --- per-type checks -------------------------------------------------

    def _numeric(self, issue, f: FieldSpec, value: str) -> list[Issue]:
        # The guide says numeric fields are zero-filled, but MV-RS and FCS accept
        # space padding on either side (user decision). Spaces inside the number are not.
        digits = value.strip()
        allowed = f.charset.chars if f.charset else set("0123456789")
        bad = set(digits) - allowed
        if bad:
            label = f.charset.label if f.charset else "numeric"
            return [issue("invalid-characters", f"{f.label}: non-{label} characters",
                          f"These bytes can only contain {label} characters, but this record contains "
                          f"{_show(value)} ({_chars(bad)} not allowed).")]
        if f.check == "rf_frequency" and not RF_FREQUENCY.match(digits):
            return [issue("rf-frequency-format", "RF Frequency format",
                          f"The RF Frequency must be a number such as 000952.00625 (p.78), but this record contains {_show(value)}.")]
        if f.values is not None and not self._in_values(digits, f.values):
            return [issue("invalid-value", f"{f.label}: invalid value",
                          f"Allowed values are {', '.join(v or 'blank' for v in f.values)}, but this record contains {_show(value)}.")]
        if f.range is not None and digits.isdigit():
            n = int(digits)
            lo, hi = f.range
            if not lo <= n <= hi:
                return [issue("out-of-range", f"{f.label}: out of range",
                              f"The value must be between {lo} and {hi}, but this record contains {_show(value)}.")]
        return []

    @staticmethod
    def _in_values(digits: str, values: tuple[str, ...]) -> bool:
        # Compare numerically so "7" (space-padded) matches "0007".
        if digits in values:
            return True
        return digits.isdigit() and int(digits) in {int(v) for v in values if v.isdigit()}

    def _date(self, issue, value: str) -> list[Issue]:
        try:
            if not value.isdigit() or len(value) != 8:
                raise ValueError
            date(int(value[4:]), int(value[:2]), int(value[2:4]))
        except ValueError:
            return [issue("invalid-date", "Invalid date",
                          f"These bytes must contain a valid date in MMDDYYYY format, but this record contains {_show(value)}.")]
        return []

    def _time(self, issue, value: str) -> list[Issue]:
        ok = value.isdigit() and len(value) == 6 and int(value[:2]) < 24 and int(value[2:4]) < 60 and int(value[4:]) < 60
        if not ok:
            return [issue("invalid-time", "Invalid time",
                          f"These bytes must contain a valid time in HHMMSS format, but this record contains {_show(value)}.")]
        return []

    def _text(self, issue, f: FieldSpec, value: str) -> list[Issue]:
        issues = []
        trimmed = value.rstrip()
        charset = f.charset
        bad = set(trimmed) - charset.chars if charset else set()
        if bad:
            issues.append(issue("invalid-characters", f"{f.label}: non-{charset.label} characters",
                                f"These bytes can only contain {charset.label} characters, but this record "
                                f"contains {_show(value)} ({_chars(bad)} not allowed)."))
        if not f.mixed_case and any(c.islower() for c in trimmed):
            issues.append(issue("lowercase", f"{f.label}: lowercase letters",
                                "This field must be uppercase (only names, addresses, messages and descriptions "
                                f"may be mixed case), but this record contains {_show(value)}."))
        if value[0] == " " and not f.leading_spaces:
            issues.append(issue("not-left-justified", f"{f.label} not left-justified",
                                f"{TYPE_LABELS[f.type].capitalize()} fields must be left-justified and blank-filled "
                                f"to the right, but this record contains \"{value}\" (starts with spaces)."))
        if f.values is not None and trimmed not in f.values and not bad:
            allowed = ", ".join(v or "blank" for v in f.values)
            issues.append(issue("invalid-value", f"{f.label}: invalid value",
                                f"Allowed values are {allowed}, but this record contains {_show(value)}."))
        if f.check == "route_number":
            issues.extend(self._route_number(issue, value))
        return issues

    def _route_number(self, issue, value: str) -> list[Issue]:
        if " " in value.strip() or len(value.strip()) != 8 or set(value) & (ROUTE_NUMBER_FORBIDDEN - {" "}):
            return [issue("route-number-format", "Route Number format",
                          "The Route Number must be exactly eight characters and cannot contain periods, "
                          f"asterisks, question marks or spaces (p.79), but this record contains \"{value}\".")]
        return []

    def _rf_frequency_blank(self, issue, data: str, value: str) -> list[Issue]:
        # Blank is allowed when Geographic Area (bytes 37-38) is used instead (p.24-25).
        geo = data[36:38]
        if geo.strip() and geo.strip("0"):
            return []
        return [issue("required-blank", "Required RF Frequency is blank",
                      "The RF Frequency is required unless a Geographic Area is given in bytes 37-38, "
                      "but both are blank or zero.")]
