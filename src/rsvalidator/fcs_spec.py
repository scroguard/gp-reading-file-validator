"""FCS element dictionary (formats/fcs_elements.yaml + formats/fcs.yaml) and value checks.

The dictionary is generated from the guide's element tables; each element's
free-text "Valid values" is turned into concrete constraints here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cache
from pathlib import Path

import yaml

from .issues import Severity

FORMATS_DIR = Path(__file__).parent / "formats"
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8, "ten": 10}


@dataclass
class Element:
    entity: str
    name: str
    type: str | None
    page: int | None
    import_rule: str | None  # "required" | "optional" | "not-used" | None
    alias: str | None = None
    length: int | None = None
    values: tuple[tuple[str, str], ...] = ()
    valid: str = ""
    child: str | None = None
    # derived from `valid`
    max_len: int | None = None
    min_len: int | None = None
    exact_len: int | None = None
    num_range: tuple[float, float] | None = None
    uppercase: bool = False
    first_not_space: bool = False
    fmt: str | None = None  # "HHMMSS" | "date" | "datetime" | "time"
    fmt_max: int | None = None
    not_all_zeros: bool = False

    @property
    def qualified(self) -> str:
        return f"{self.entity}.{self.name}"

    @property
    def required(self) -> bool:
        return self.import_rule == "required"


@dataclass
class Entity:
    name: str
    page: int
    elements: list[Element]
    children: list[str] = field(default_factory=list)  # child entities, in element order
    export_only: frozenset[str] = frozenset()  # documented only for export; ignored on import

    def element(self, name: str) -> Element | None:
        for e in self.elements:
            if not e.child and (e.name == name or e.alias == name):
                return e
        return None

    def position(self, name: str) -> int | None:
        for i, e in enumerate(self.elements):
            if e.name == name or e.alias == name or e.child == name:
                return i
        return None


@dataclass
class FcsSpec:
    format: str
    guide: str
    entities: dict[str, Entity]
    rules: dict


def _number(text: str) -> float:
    return float(text.replace(",", "").replace(" ", ""))


def _derive(el: Element):
    v = el.valid
    low = v.lower()
    if el.type == "String" and el.length:
        el.max_len = el.length
    m = re.search(r"maximum(?: of)? (\d+|\w+) characters?", low)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else WORDS.get(m.group(1))
        if n:
            el.max_len = n
    m = re.search(r"exactly (\d+) characters", low)
    if m:
        el.exact_len = int(m.group(1))
    m = re.search(r"minimum of (\d+) characters", low)
    if m:
        el.min_len = int(m.group(1))
    m = re.match(r"(?:numeric,\s*)?(-?\d[\d, ]*?)\s*(?:-|through)\s*(-?\d[\d,]*(?:,\s\d{3})*)", v, re.I)
    if m and el.type in ("Integer", "Byte", "String", "Float", "Double"):
        el.num_range = (_number(m.group(1)), _number(m.group(2)))
    el.uppercase = "must be uppercase" in low
    el.first_not_space = "first character cannot be a space" in low or "first character can not be a space" in low
    m = re.search(r"format = hhmmss\. maximum of (\d+)", low)
    if m:
        el.fmt, el.fmt_max = "HHMMSS", int(m.group(1))
    elif "format = hh:mm:ss" in low:
        el.fmt = "time"
    elif re.search(r"format = yyyy-mm-ddthh:mm:ss", low) and el.type == "DateTime":
        el.fmt = "datetime"
    elif "format = yyyy-mm-dd" in low or el.type == "Date":
        el.fmt = "date"
    elif el.type == "DateTime":
        el.fmt = "datetime"


@cache
def load_fcs() -> FcsSpec:
    gen = yaml.safe_load((FORMATS_DIR / "fcs_elements.yaml").read_text())["entities"]
    rules = yaml.safe_load((FORMATS_DIR / "fcs.yaml").read_text())
    entities: dict[str, Entity] = {}
    for name, raw in gen.items():
        elements = []
        for r in raw["elements"]:
            el = Element(
                entity=name, name=r["name"], type=r.get("type"), page=r.get("page"),
                import_rule=r.get("import"), alias=r.get("alias"), length=r.get("length"),
                values=tuple((str(x["value"]), x["meaning"]) for x in r.get("values", [])),
                valid=r.get("valid", ""), child=r.get("child"),
            )
            _derive(el)
            elements.append(el)
        entities[name] = Entity(name, raw["page"], elements, export_only=frozenset(raw.get("export_only", [])))
    for ent in entities.values():
        # Drop references to entities the import chapter does not define
        # (e.g. MeterSessionInputChangedFields, an export entity).
        ent.children = [e.child for e in ent.elements if e.child in entities]
    for parent, kids in rules.get("xml", {}).get("extra_children", {}).items():
        for k in kids:
            if k not in entities[parent].children:
                entities[parent].children.append(k)
    for fix in rules.get("overrides", []):
        el = entities[fix["entity"]].element(fix["element"])
        for key, val in fix.items():
            if key == "drop_values":
                el.values = tuple(v for v in el.values if v[0] not in val)
            elif key == "values":
                el.values = tuple(tuple(x) for x in val)
            elif key == "num_range":
                el.num_range = tuple(val)
            elif key not in ("entity", "element", "note"):
                setattr(el, key, val)
    return FcsSpec(format=rules["format"], guide=rules["guide"], entities=entities, rules=rules)


# --- value checks ---------------------------------------------------------------

BOOLEANS = {"true", "false", "True", "False"}
GUID = re.compile(r"^(\{)?[0-9A-Fa-f]{8}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{12}(?(1)\})$")


@dataclass
class Problem:
    code: str
    summary: str
    reason: str
    severity: Severity = Severity.ERROR


def _enum_match(value: str, el: Element) -> bool:
    for token, _ in el.values:
        if value == token:
            return True
        m = re.fullmatch(r"(\d+)-(\d+)", token)
        if m and value.isdigit() and int(m.group(1)) <= int(value) <= int(m.group(2)):
            return True
        if el.type in ("Integer", "Byte") and token.lstrip("-").isdigit() and re.fullmatch(r"-?\d+", value):
            if int(token) == int(value):
                return True
        if el.type == "Boolean" and token.lower() == value.lower():
            return True
    return False


def _allowed(el: Element) -> str:
    return ", ".join(f"{t or 'blank'} ({m})" if m else (t or "blank") for t, m in el.values)


def check_value(el: Element, value: str, *, csv: bool = False) -> list[Problem]:
    """Check one non-empty value against its element definition."""
    probs: list[Problem] = []
    label = el.qualified
    t = el.type

    if t == "String" or t is None:
        if value != value.rstrip():
            probs.append(Problem("trailing-spaces", f"{label}: trailing spaces",
                                 "String values should not be padded with blanks at the end (p.12).", Severity.WARNING))
        text = value.rstrip()
        if el.exact_len and len(text) != el.exact_len:
            probs.append(Problem("length", f"{label}: wrong length",
                                 f"It must be exactly {el.exact_len} characters, but \"{text}\" is {len(text)}."))
        elif el.max_len and len(text) > el.max_len:
            probs.append(Problem("length", f"{label}: too long",
                                 f"It can be at most {el.max_len} characters, but \"{text}\" is {len(text)}."))
        if el.min_len and len(text) < el.min_len:
            probs.append(Problem("length", f"{label}: too short",
                                 f"It must be at least {el.min_len} characters, but \"{text}\" is {len(text)}."))
        if el.uppercase and text != text.upper():
            probs.append(Problem("lowercase", f"{label}: lowercase letters",
                                 f"Letters must be uppercase, but the value is \"{text}\"."))
        if el.first_not_space and value[:1] == " ":
            probs.append(Problem("leading-space", f"{label}: starts with a space",
                                 "The first character cannot be a space."))
        if el.fmt == "HHMMSS":
            ok = re.fullmatch(r"\d{6}", text) and int(text[2:4]) < 60 and int(text[4:]) < 60 and int(text) <= (el.fmt_max or 995959)
            if not ok:
                probs.append(Problem("invalid-format", f"{label}: not HHMMSS",
                                     f"It must be a duration in HHMMSS format, at most {el.fmt_max or 995959}, but the value is \"{text}\"."))
        elif el.num_range and not el.values:
            probs += _range(el, text, label)
        if el.not_all_zeros and text and set(text) == {"0"}:
            probs.append(Problem("all-zeros", f"{label}: all zeros", "It cannot be all zeros."))
    elif t in ("Integer", "Byte"):
        if not re.fullmatch(r"-?\d+", value):
            return [Problem("not-integer", f"{label}: not a whole number",
                            f"It must be a whole number with no spaces or separators, but the value is \"{value}\".")]
        if el.length and len(value.lstrip("-")) > el.length and not el.num_range:
            probs.append(Problem("length", f"{label}: too many digits",
                                 f"It can have at most {el.length} digit(s), but the value is \"{value}\"."))
        if el.num_range and not el.values:
            probs += _range(el, value, label)
        elif t == "Byte" and not el.values and not 0 <= int(value) <= 255:
            probs.append(Problem("out-of-range", f"{label}: out of range",
                                 f"A Byte value must be between 0 and 255, but the value is \"{value}\"."))
    elif t in ("Double", "Float"):
        number = value.replace(",", ".") if csv else value  # CSV accepts , as decimal separator (p.4)
        if not re.fullmatch(r"-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", number):
            return [Problem("not-number", f"{label}: not a number",
                            f"It must be a number, but the value is \"{value}\".")]
        if el.num_range:
            probs += _range(el, number, label)
    elif t == "Boolean":
        if value not in BOOLEANS:
            return [Problem("not-boolean", f"{label}: not true/false",
                            f"It must be true or false, but the value is \"{value}\".")]
    elif t in ("Date", "DateTime") or el.fmt in ("date", "datetime", "time"):
        probs += _datetime(el, value, label)
    elif t == "FilteringInformationType":
        if len(value) > 411:
            probs.append(Problem("length", f"{label}: too long",
                                 f"Filtering information can be at most 411 characters, but it is {len(value)}."))
        if not value.startswith("/Root Node"):
            probs.append(Problem("filtering-root", f"{label}: does not start with /Root Node",
                                 f"Itron recommends starting filtering information with \"/Root Node\" (p.17), "
                                 f"and it must match the organizational hierarchy exactly. The value is \"{value}\".",
                                 Severity.WARNING))
    elif t == "GUID":
        if not GUID.match(value):
            probs.append(Problem("not-guid", f"{label}: not a GUID", f"It must be a GUID, but the value is \"{value}\"."))

    if el.values and not probs and not _enum_match(value.strip() if t == "String" else value, el):
        probs.append(Problem("invalid-value", f"{label}: invalid value",
                             f"Allowed values are {_allowed(el)}, but the value is \"{value}\"."))
    return probs


def _range(el: Element, text: str, label: str) -> list[Problem]:
    try:
        n = float(text)
    except ValueError:
        return [Problem("not-number", f"{label}: not a number",
                        f"It must be a number from {_fmt(el.num_range[0])} to {_fmt(el.num_range[1])}, but the value is \"{text}\".")]
    lo, hi = el.num_range
    if not lo <= n <= hi:
        return [Problem("out-of-range", f"{label}: out of range",
                        f"It must be from {_fmt(lo)} to {_fmt(hi)}, but the value is \"{text}\".")]
    return []


def _fmt(n: float) -> str:
    return f"{int(n):,}" if n == int(n) else str(n)


def _datetime(el: Element, value: str, label: str) -> list[Problem]:
    fmt = el.fmt or ("date" if el.type == "Date" else "datetime")
    patterns = {"date": ("%Y-%m-%d", "yyyy-mm-dd"), "datetime": ("%Y-%m-%dT%H:%M:%S", "yyyy-mm-ddThh:mm:ss"),
                "time": ("%H:%M:%S", "hh:mm:ss")}
    pyfmt, shown = patterns[fmt]
    try:
        datetime.strptime(value, pyfmt)
        if fmt == "date":
            date.fromisoformat(value)
    except ValueError:
        return [Problem("invalid-date", f"{label}: invalid {'date' if fmt == 'date' else 'date/time' if fmt == 'datetime' else 'time'}",
                        f"It must be a valid value in {shown} format, but the value is \"{value}\".")]
    return []
