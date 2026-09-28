"""Automatic correction of Temetra CSV rows that repeat a meter as a second register.

A kWh/kW (or other multi-register) meter exported as two rows with the same CREF and
METERSERIAL loses the earlier row on import: the repeated CREF overwrites it. Itron's
annotated example, confirmed by importing a customer file, gives each extra register:

- a suffixed CREF and METERSERIAL (116-1 / 3333333-1, then -2 ...),
- LINKEDMETERSERIAL set to the original meter serial, and
- an original-meter-serial=<serial> tag in ADDTAG.

Rows that are not corrected are copied byte-for-byte, except that missing LINKEDMETERSERIAL
or ADDTAG columns are added (empty) to every row.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from .fcs_csv import decode

TAG = "original-meter-serial"


@dataclass
class Correction:
    line: int
    cref: str
    serial: str
    new_cref: str
    new_serial: str
    main_line: int
    changes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (f"Line {self.line} is a second register of meter {self.serial} (line {self.main_line}): "
                + "; ".join(self.changes) + ".")


@dataclass
class FixResult:
    content: bytes
    filename: str
    corrections: list[Correction]
    added_columns: list[str]


def corrected_name(filename: str) -> str:
    p = Path(filename or "upload.csv")
    return f"{p.stem}-corrected{p.suffix or '.csv'}"


def fix_register_duplicates(content: bytes, filename: str) -> FixResult | None:
    """Return a corrected file, or None when there is nothing to correct."""
    text, problems = decode(content)
    if problems:
        return None
    physical = text.splitlines(keepends=True)
    if not physical:
        return None

    # Read logical CSV rows, remembering which physical lines each came from.
    rows: list[tuple[int, int, list[str]]] = []  # (first line, last line, values), 1-based
    reader = csv.reader(iter(physical))
    consumed = 0
    try:
        for values in reader:
            rows.append((consumed + 1, reader.line_num, values))
            consumed = reader.line_num
    except csv.Error:
        return None
    if len(rows) < 2:
        return None

    header = rows[0][2]
    names = [h.strip().upper() for h in header]
    if "CREF" not in names or "METERSERIAL" not in names:
        return None
    ci, si = names.index("CREF"), names.index("METERSERIAL")
    ii = names.index("IGNORE") if "IGNORE" in names else None
    added = [c for c in ("LINKEDMETERSERIAL", "ADDTAG") if c not in names]
    width = len(header) + len(added)
    li = names.index("LINKEDMETERSERIAL") if "LINKEDMETERSERIAL" in names else len(header) + added.index("LINKEDMETERSERIAL")
    ti = names.index("ADDTAG") if "ADDTAG" in names else len(header) + added.index("ADDTAG")

    def cell(values, i):
        return values[i].strip() if i < len(values) else ""

    used_crefs = {cell(v, ci) for _, _, v in rows[1:]}
    used_serials = {cell(v, si) for _, _, v in rows[1:]}
    first: dict[str, tuple[int, str]] = {}  # CREF -> (line, serial) of its first row
    corrections: list[Correction] = []
    new_values: dict[int, list[str]] = {}

    for start, _, values in rows[1:]:
        if ii is not None and cell(values, ii).lower() in ("yes", "true"):
            continue
        cref, serial = cell(values, ci), cell(values, si)
        if not cref or not any(v.strip() for v in values):
            continue
        if cref not in first:
            first[cref] = (start, serial)
            continue
        main_line, main_serial = first[cref]
        if serial != main_serial or not serial:
            continue  # a different meter sharing the CREF: not a second register, left for the customer
        k = 1
        while f"{cref}-{k}" in used_crefs or f"{serial}-{k}" in used_serials:
            k += 1
        new_cref, new_serial = f"{cref}-{k}", f"{serial}-{k}"
        used_crefs.add(new_cref)
        used_serials.add(new_serial)
        out = list(values) + [""] * (width - len(values))
        old_link, old_tags = out[li].strip(), out[ti].strip()
        out[ci], out[si], out[li] = new_cref, new_serial, main_serial
        tags = old_tags.split()
        if not any(t.startswith(TAG + "=") for t in tags):
            tags.append(f"{TAG}={main_serial}")
        out[ti] = " ".join(tags)
        fix = Correction(start, cref, serial, new_cref, new_serial, main_line)
        fix.changes = [f"CREF {cref} → {new_cref}", f"METERSERIAL {serial} → {new_serial}",
                       f"LINKEDMETERSERIAL {'set to' if not old_link else old_link + ' →'} {main_serial}",
                       f"ADDTAG: added {TAG}={main_serial}"]
        corrections.append(fix)
        new_values[start] = out

    if not corrections:
        return None

    def to_csv(values: list[str]) -> str:
        buf = io.StringIO()
        csv.writer(buf, lineterminator="").writerow(values)
        return buf.getvalue()

    pieces = []
    for start, end, values in rows:
        original = "".join(physical[start - 1:end])
        ending = original[len(original.rstrip("\r\n")):]
        if start in new_values:
            pieces.append(to_csv(new_values[start]) + ending)
        elif added and any(v.strip() for v in values):
            # Missing LINKEDMETERSERIAL/ADDTAG columns are added: headings on row 1, empty values elsewhere.
            extra = ",".join(added) if start == 1 else "," * (len(added) - 1)
            pieces.append(original.rstrip("\r\n") + "," + extra + ending)
        else:
            pieces.append(original)
    body = "".join(pieces)
    trailing = "".join(physical[rows[-1][1]:])  # blank lines after the last row
    encoding = "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    bom = content.startswith(b"\xef\xbb\xbf")
    data = (body + trailing).encode(encoding)
    if bom:
        data = b"\xef\xbb\xbf" + data
    return FixResult(data, corrected_name(filename), corrections, added)
