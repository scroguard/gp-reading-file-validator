"""The structured problem type every validation layer emits."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"  # must be corrected: the import fails or data is wrong
    RECOMMENDED = "recommended"  # highly recommend correcting: accepted by some target systems, not others
    WARNING = "warning"  # for reference

    @property
    def label(self) -> str:
        return {"error": "Must correct", "recommended": "Highly recommended", "warning": "Warning"}[self.value]

    @property
    def rank(self) -> int:
        return {"error": 0, "recommended": 1, "warning": 2}[self.value]


@dataclass
class Issue:
    severity: Severity
    code: str  # stable rule identifier, used to group the summary
    message: str  # complete sentence shown to the user
    line: int | None = None  # 1-based line number in the uploaded file
    record: str | None = None  # record ID, e.g. "CUS"
    start: int | None = None  # 1-based byte range within the line
    end: int | None = None
    field: str | None = None  # field label, e.g. "Account Number"
    page: int | None = None  # interface guide page for the rule
    summary: str | None = None  # short category label for the summary table
    column: int | None = None  # 1-based CSV column
    path: str | None = None  # XML element path, e.g. "WorkSet/Work/Customer/Meter"
    owner: tuple[str, int] | None = None  # report section, when not implied by the line
    fix: str | None = None  # suggested correction, shown as "How to fix"

    @property
    def position_label(self) -> str | None:
        if self.start is not None:
            return self.bytes_label
        if self.column is not None:
            return f"column {self.column}"
        return None

    @property
    def bytes_label(self) -> str | None:
        if self.start is None:
            return None
        if self.end is None or self.end == self.start:
            return f"byte {self.start}"
        return f"bytes {self.start}-{self.end}"

    @property
    def category(self) -> str:
        return self.summary or self.code
