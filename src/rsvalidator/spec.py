"""Loads format definitions (record layouts) from the YAML files in `formats/`."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache, cached_property
from pathlib import Path

import yaml

FORMATS_DIR = Path(__file__).parent / "formats"

TYPE_LABELS = {
    "A": "alphabetic",
    "AN": "alphanumeric",
    "N": "numeric",
    "D": "date (MMDDYYYY)",
    "T": "time (HHMMSS)",
}


@dataclass(frozen=True)
class Charset:
    key: str
    label: str
    chars: frozenset[str]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    start: int  # 1-based, inclusive
    end: int  # 1-based, inclusive
    type: str
    required: bool | str = False  # True, False, or "trailer"
    values: tuple[str, ...] | None = None
    charset: Charset | None = None
    mixed_case: bool = False
    range: tuple[int, int] | None = None
    pad: bool = False
    reserved: bool = False
    leading_spaces: bool = False
    check: str | None = None
    page: int | None = None
    note: str | None = None

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def slice(self, data: str) -> str:
        return data[self.start - 1 : self.end]

    def bytes_label(self) -> str:
        if self.start == self.end:
            return f"byte {self.start}"
        return f"bytes {self.start}-{self.end}"

    def is_required(self, record_id: str, trailer_ids: frozenset[str]) -> bool:
        if self.required == "trailer":
            return record_id in trailer_ids
        return bool(self.required)


@dataclass(frozen=True)
class RecordSpec:
    ids: tuple[str, ...]
    name: str
    fields: tuple[FieldSpec, ...]
    page: int | None = None

    def field(self, name: str) -> FieldSpec:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(f"{self.name} has no field {name!r}")


@dataclass(frozen=True)
class FormatSpec:
    key: str
    format: str
    guide: str
    line_length: int
    data_length: int
    charsets: dict[str, Charset]
    records: tuple[RecordSpec, ...]
    trailer_ids: frozenset[str]
    rules: dict = field(default_factory=dict)

    def record(self, record_id: str) -> RecordSpec | None:
        return self._by_id.get(record_id)

    @cached_property
    def _by_id(self) -> dict[str, RecordSpec]:
        return {rid: r for r in self.records for rid in r.ids}

    def record_id_of(self, data: str) -> str:
        """Return the record ID at the start of a line, or the raw 3 bytes if unknown.

        Table records use two-letter IDs in the three-byte field (guide p.77).
        """
        three = data[:3]
        if self.record(three):
            return three
        two = data[:2]
        if self.record(two) and (len(data) < 3 or data[2] == " "):
            return two
        return three


def _charset_for(ftype: str, override: str | None, charsets: dict[str, Charset]) -> Charset | None:
    if override:
        return charsets[override]
    return charsets.get(ftype)


def _field(raw: dict, charsets: dict[str, Charset]) -> FieldSpec:
    values = raw.get("values")
    rng = raw.get("range")
    return FieldSpec(
        name=raw["name"],
        label=raw["label"],
        start=raw["start"],
        end=raw["end"],
        type=raw["type"],
        required=raw.get("required", False),
        values=tuple(str(v) for v in values) if values is not None else None,
        charset=_charset_for(raw["type"], raw.get("charset"), charsets),
        mixed_case=raw.get("mixed_case", False),
        range=(rng[0], rng[1]) if rng else None,
        pad=raw.get("pad", False),
        reserved=raw.get("reserved", False),
        leading_spaces=raw.get("leading_spaces", False),
        check=raw.get("check"),
        page=raw.get("page"),
        note=raw.get("note"),
    )


@cache
def load_format(key: str) -> FormatSpec:
    raw = yaml.safe_load((FORMATS_DIR / f"{key}.yaml").read_text())
    charsets = {
        k: Charset(key=k, label=v["label"], chars=frozenset(v["chars"]))
        for k, v in raw["charsets"].items()
    }
    records = tuple(
        RecordSpec(
            ids=tuple(r["ids"]),
            name=r["name"],
            page=r.get("page"),
            fields=tuple(_field(f, charsets) for f in r["fields"]),
        )
        for r in raw["records"]
    )
    return FormatSpec(
        key=key,
        format=raw["format"],
        guide=raw["guide"],
        line_length=raw["line_length"],
        data_length=raw["data_length"],
        charsets=charsets,
        records=records,
        trailer_ids=frozenset(raw.get("trailer_ids", [])),
        rules=raw.get("rules", {}),
    )
