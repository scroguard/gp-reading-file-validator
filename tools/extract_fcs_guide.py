"""Generate formats/fcs_elements.yaml from the FCS CSV and XML File Format Reference Guide.

Usage:
    pdftotext -layout FCS_..._Reference_Guide.pdf guide.txt
    python tools/extract_fcs_guide.py guide.txt src/rsvalidator/formats/fcs_elements.yaml

Reads the "XML Import File" chapter (printed pages 15-108; printed page = PDF
page - 10) and writes every entity table: element name, type, length, valid
values, and XML Import requirement. The output is data for the validator and
should be reviewed by hand after regenerating; anything the parser could not
interpret is kept in `valid` (free text) or flagged with `review:`.
"""

from __future__ import annotations

import re
import sys

import yaml

FIRST_PAGE, LAST_PAGE, PAGE_OFFSET = 15, 108, 9  # printed pages; index = printed + offset
EXPORT_FIRST, EXPORT_LAST = 109, 244  # "XML Export File" chapter, for export-only element names

CAPTION = re.compile(r"^\s*([A-Z][A-Za-z0-9]*)\s*$")
HEADER = re.compile(r"^\s*Element\s{2,}Description\s*$")
HEADER_VARIANT = re.compile(r"^\s*([A-Z][A-Za-z0-9]*)\s{2,}Description\s*$")  # "<Entity>  Description" / "Element"
NAME_ROW = re.compile(r"^(\s*)([A-Za-z][A-Za-z0-9_]*)\s{2,}(\S.*)$")
ALIAS = re.compile(r"^\s*\(([A-Za-z][A-Za-z0-9_]*)\)")
NOISE = ("Proprietary and Confidential", "File Format Reference Guide")
TYPES = {
    "string": "String", "integer": "Integer", "long": "Integer", "byte": "Byte", "boolean": "Boolean",
    "date": "Date", "datetime": "DateTime", "double": "Double", "float": "Float",
    "filteringinformationtype": "FilteringInformationType", "guid": "GUID",
}


def read_tables(pages: list[str], first: int = FIRST_PAGE, last: int = LAST_PAGE) -> list[tuple[str, int, list[dict]]]:
    tables: dict[str, tuple[int, list[dict]]] = {}
    last_heading = None
    for printed in range(first, last + 1):
        lines = [ln for ln in pages[printed + PAGE_OFFSET].splitlines() if not any(n in ln for n in NOISE)]
        first = next((k for k, ln in enumerate(lines) if ln.strip()), None)
        lines = lines[first + 1:] if first is not None else []  # drop running page header
        i = 0
        while i < len(lines):
            m = CAPTION.match(lines[i])
            if m:
                last_heading = m.group(1)
            v = HEADER_VARIANT.match(lines[i])
            if v and i + 1 < len(lines) and lines[i + 1].strip() == "Element":
                last_heading, desc_col, i = v.group(1), lines[i].index("Description"), i + 2
            elif HEADER.match(lines[i]) and last_heading:
                desc_col, i = lines[i].index("Description"), i + 1
            else:
                i += 1
                continue
            page, rows = tables.setdefault(last_heading, (printed, []))
            while i < len(lines):
                line = lines[i]
                indent = len(line) - len(line.lstrip())
                left, right = line[:desc_col - 1].strip(), line[desc_col - 2:].rstrip()
                nm = NAME_ROW.match(line)
                if not line.strip():
                    pass
                elif rows and ALIAS.match(left):
                    rows[-1]["alias"] = ALIAS.match(left).group(1)
                    rows[-1]["text"].append(right)
                elif nm and indent < desc_col - 10 and line.find(nm.group(3)) >= desc_col - 2:
                    rows.append({"name": nm.group(2), "page": printed, "text": [nm.group(3)]})
                elif indent >= desc_col - 2 and rows:
                    rows[-1]["text"].append(right)
                else:
                    break
                i += 1
    return [(name, page, rows) for name, (page, rows) in tables.items()]


def parse_element(row: dict) -> dict:
    raw = row["text"]
    text = " ".join(x.strip() for x in raw)
    el: dict = {"name": row["name"]}
    if "alias" in row:
        el["alias"] = row["alias"]

    child = re.match(r"See ([A-Z][A-Za-z0-9]+)\b", text)
    if child and "Type/length" not in text:
        el["child"] = child.group(1)

    m = re.search(r"Type/length:\s*(?:Valid values:\s*)?([A-Za-z]+)\s*(?:\((\d+)\))?", text)
    if m and m.group(1).lower() in TYPES:
        el["type"] = TYPES[m.group(1).lower()]
        if m.group(2):
            el["length"] = int(m.group(2))

    values, valid, in_vv = [], [], False
    for line in raw:
        s = line.strip()
        if s.startswith("Valid values:"):
            in_vv = True
            rest = s[len("Valid values:"):].strip()
            if rest and rest.lower() not in TYPES:
                valid.append(rest)
            continue
        if not in_vv:
            continue
        if re.match(r"(Usage|XML Import|XMl Import|XML Export|Note|Type/length)\b", s):
            in_vv = False
            continue
        vm = re.match(r"^ {1,4}(\S+)\s+(\S.*)$", line)
        if vm and not valid and vm.group(1) not in ("-",):
            values.append([vm.group(1), vm.group(2).strip()])
        elif values:
            values[-1][1] += " " + s
        else:
            valid.append(s)
    if values:
        el["values"] = [{"value": "" if v == "blank" else v, "meaning": d} for v, d in values]
    if valid:
        el["valid"] = " ".join(valid)

    m = re.search(r"XML Import:\s*(Required|Optional|Not used for import|Not used)", text, re.I)
    el["import"] = {"required": "required", "optional": "optional"}.get(m.group(1).lower(), "not-used") if m else None
    if el["import"] is None:
        el["review"] = "XML Import requirement not stated"
        del el["import"]

    if "type" not in el and "child" not in el:
        if values and all(re.fullmatch(r"-?\d+(-\d+)?", v[0]) for v in values):
            el["type"] = "Integer"
            el["review"] = "type not stated in guide; inferred Integer from valid values"
        elif values and {v[0].lower() for v in values} <= {"true", "false"}:
            el["type"] = "Boolean"
            el["review"] = "type not stated in guide; inferred Boolean from valid values"
        elif valid and valid[0].lower().startswith("alphanumeric"):
            el["type"] = "String"
            el["review"] = "type not stated in guide; inferred String from valid values"
        elif "not used" not in text.lower():
            el["review"] = "type not stated in guide"
    el["page"] = row["page"]
    return el


def main(src: str, dest: str):
    pages = open(src, encoding="utf-8").read().split("\f")
    out = {"entities": {}}
    for name, page, rows in read_tables(pages):
        out["entities"][name] = {"page": page, "elements": [parse_element(r) for r in rows]}
    # Elements documented only for export (FCS fills them in); seen in real import files.
    for name, page, rows in read_tables(pages, EXPORT_FIRST, EXPORT_LAST):
        if name not in out["entities"]:
            continue
        known = {e["name"] for e in out["entities"][name]["elements"]} | {
            e.get("alias") for e in out["entities"][name]["elements"]}
        extra = [r["name"] for r in rows if r["name"] not in known]
        if extra:
            out["entities"][name]["export_only"] = sorted(set(extra))
    header = (
        "# GENERATED by tools/extract_fcs_guide.py from the FCS CSV and XML File Format\n"
        "# Reference Guide (TDC-1664-002), chapter \"XML Import File\". Page numbers are the\n"
        "# guide's printed pages. Hand corrections belong in formats/fcs.yaml, not here.\n"
    )
    with open(dest, "w") as f:
        f.write(header)
        yaml.safe_dump(out, f, sort_keys=False, width=110, allow_unicode=False)
    for name, ent in out["entities"].items():
        flagged = sum("review" in e for e in ent["elements"])
        print(f"{name:28} p.{ent['page']:<4} {len(ent['elements']):3} elements  {flagged} to review")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
