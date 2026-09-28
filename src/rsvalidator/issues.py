"""The structured problem type every validation layer emits."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


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
