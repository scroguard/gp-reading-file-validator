"""FCS XML import file validation (guide chapters 2-3, printed pages 9-108).

Checks well-formedness, namespace, section and code-collection order, each
entity's elements (known, in the documented order, required ones present,
values valid) and the cross-entity rules for meter session inputs.

The guide does not name the root element or publish the XSDs; element order
comes from the order of the guide's element tables.
"""

from __future__ import annotations

from lxml import etree

from .fcs_spec import Entity, FcsSpec, check_value, load_fcs
from .grouping import Grouping
from .issues import Issue, Severity


def _local(tag) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _ns(tag) -> str | None:
    return etree.QName(tag).namespace if isinstance(tag, str) else None


class XmlChecker:
    def __init__(self, spec: FcsSpec):
        self.spec = spec
        self.rules = spec.rules["xml"]
        self.issues: list[Issue] = []
        self.grouping = Grouping()
        self.route_index = -1
        self.bad_namespace_reported = False

    def issue(self, el, code, summary, message, severity=Severity.ERROR, page=None, path=None, field=None):
        line = el.sourceline if el is not None else None
        self.issues.append(Issue(severity=severity, code=code, summary=summary, line=line,
                                 path=path, field=field, page=page,
                                 message=(f"Line {line}: " if line else "") + message))

    def path_of(self, el) -> str:
        parts = []
        while el is not None and el.getparent() is not None:
            parts.append(_local(el.tag))
            el = el.getparent()
        return "/".join(reversed(parts))

    def children(self, el):
        return [c for c in el if isinstance(c.tag, str)]

    def own(self, el, key):
        for d in el.iter():
            if isinstance(d.tag, str) and d.sourceline:
                self.grouping.owner[d.sourceline] = key

    # --- document --------------------------------------------------------

    def run(self, root):
        self.check_namespaces(root)
        order = self.rules["sections"]
        last = -1
        for child in self.children(root):
            name = _local(child.tag)
            if name not in order:
                self.issue(child, "unknown-element", "Unknown element",
                           f"<{name}> is not allowed here. The import file contains only <Codes>, <Messages> and "
                           f"<WorkSets> sections (p.15).", path=self.path_of(child), page=15)
                continue
            pos = order.index(name)
            if pos <= last:
                self.issue(child, "element-order", "Element out of order",
                           f"<{name}> is out of order or repeated. The sections must appear in the order "
                           f"{', '.join(order)} (p.15).", path=self.path_of(child), page=15)
            last = max(last, pos)
            self.check_attributes(child)
            if name == "Codes":
                self.codes(child)
            elif name == "Messages":
                self.collection(child, self.rules["messages"]["entity"])
            else:
                self.worksets(child)
        if not any(_local(c.tag) in order for c in self.children(root)):
            self.issue(root, "no-sections", "No import sections",
                       f"The root element <{_local(root.tag)}> contains none of <Codes>, <Messages> or <WorkSets>, "
                       f"so there is nothing to import (p.15).", page=15)

    def check_namespaces(self, root):
        expected = self.rules["namespace"]
        for el in root.iter():
            if isinstance(el.tag, str) and _ns(el.tag) != expected:
                found = _ns(el.tag) or "no namespace"
                self.issue(el, "namespace", "Wrong XML namespace",
                           f"<{_local(el.tag)}> is in {found}, but every element of an FCS XML file must be in the "
                           f"namespace {expected} (p.10). Add xmlns=\"{expected}\" to the root element.",
                           path=self.path_of(el), page=10)
                return

    def check_attributes(self, el):
        allowed = self.rules["attributes"]
        for name, value in el.attrib.items():
            local = _local(name)
            if local not in allowed:
                continue  # namespace declarations and schema hints are fine
            vals = allowed[local].get("values")
            if vals and value not in vals:
                self.issue(el, "invalid-value", f"{local} attribute: invalid value",
                           f"The {local} attribute on <{_local(el.tag)}> must be true or false, but it is \"{value}\".",
                           path=self.path_of(el), page=20)

    def codes(self, codes_el):
        order = [c["collection"] for c in self.rules["codes"]]
        entity_of = {c["collection"]: c["entity"] for c in self.rules["codes"]}
        last = -1
        for child in self.children(codes_el):
            name = _local(child.tag)
            if name not in entity_of:
                self.issue(child, "unknown-element", "Unknown element",
                           f"<{name}> is not a code collection. <Codes> may contain {', '.join(order)} (p.19).",
                           path=self.path_of(child), page=19)
                continue
            pos = order.index(name)
            if pos <= last:
                self.issue(child, "element-order", "Element out of order",
                           f"<{name}> is out of order or repeated. Code collections must appear in this order: "
                           f"{', '.join(order)} (p.19).", path=self.path_of(child), page=19)
            last = max(last, pos)
            self.check_attributes(child)
            self.collection(child, entity_of[name])

    def collection(self, coll_el, entity: str):
        for child in self.children(coll_el):
            if _local(child.tag) != entity:
                self.issue(child, "unknown-element", "Unknown element",
                           f"<{_local(child.tag)}> is not allowed in <{_local(coll_el.tag)}>, which may only contain "
                           f"<{entity}> entries.", path=self.path_of(child))
                continue
            self.entity(child, self.spec.entities[entity])

    def worksets(self, ws_el):
        entity = self.rules["worksets"]["entity"]
        for child in self.children(ws_el):
            if _local(child.tag) != entity:
                self.issue(child, "unknown-element", "Unknown element",
                           f"<{_local(child.tag)}> is not allowed in <WorkSets>, which may only contain <WorkSet> "
                           f"entries (p.50).", path=self.path_of(child), page=50)
                continue
            route = (child.findtext(f"{{{self.rules['namespace']}}}WorkSetID") or child.findtext("WorkSetID") or "").strip()
            self.route_index = len(self.grouping.routes)
            self.grouping.routes.append((f"Route {route or '(unknown)'}, starting on line {child.sourceline}",
                                         child.sourceline))
            self.own(child, ("route", self.route_index))
            self.entity(child, self.spec.entities[entity])

    # --- entities --------------------------------------------------------

    def entity(self, el, ent: Entity):
        if ent.name == "Customer":
            self.start_account(el)
        extra = set(self.rules.get("extra_children", {}).get(ent.name, []))
        seen: dict[str, object] = {}
        last_pos, last_name = -1, None
        for child in self.children(el):
            name = _local(child.tag)
            path = self.path_of(child)
            is_child_entity = name in ent.children
            element = None if is_child_entity else ent.element(name)
            if element is None and not is_child_entity and name in ent.export_only:
                self.issue(child, "export-only", "Export-only element",
                           f"<{name}> is an export element of <{ent.name}>: FCS sets it itself, so it is ignored on "
                           f"import and can be removed.", Severity.WARNING, path=path)
                continue
            if element is None and not is_child_entity:
                self.issue(child, "unknown-element", "Unknown element",
                           f"<{name}> is not an element of <{ent.name}> in the guide (p.{ent.page}). Check the "
                           f"spelling and capitalization; XML tags are case-sensitive (p.10).",
                           path=path, page=ent.page)
                continue
            if name not in extra:
                pos = ent.position(name)
                if pos is not None and pos < last_pos:
                    self.issue(child, "element-order", "Element out of order",
                               f"<{name}> must come before <{last_name}> inside <{ent.name}>. The order of elements "
                               f"is required for validation (p.10).", path=path, page=10)
                elif pos is not None:
                    last_pos, last_name = pos, name
            if is_child_entity:
                self.entity(child, self.spec.entities[name])
                seen.setdefault(name, child)
                continue
            if name in seen:
                self.issue(child, "duplicate-element", "Element repeated",
                           f"<{name}> appears more than once inside <{ent.name}> (first on line "
                           f"{seen[name].sourceline}).", path=path, page=element.page)
            seen.setdefault(name, child)
            self.value(child, element, path)

        for element in ent.elements:
            target = element.child or element.name
            if element.required and target not in seen and (element.alias is None or element.alias not in seen):
                what = f"<{target}> entity" if element.child else f"<{target}> element"
                self.issue(el, "required-missing", "Required element missing",
                           f"<{ent.name}> has no {what}, which is required for import (p.{element.page}).",
                           path=self.path_of(el), page=element.page, field=f"{ent.name}.{target}")
        if ent.name == "MeterSessionInput":
            self.session_input(el)

    def value(self, child, element, path):
        if self.children(child):
            self.issue(child, "unexpected-children", "Element has child elements",
                       f"<{element.name}> holds a value and cannot contain other elements.", path=path, page=element.page)
            return
        text = child.text or ""
        label = element.qualified
        if element.import_rule == "not-used":
            self.issue(child, "not-used", f"{label} is not used for import",
                       f"The guide marks {label} as not used for import, so this value is ignored.",
                       Severity.WARNING, element.page, path, label)
            return
        forbidden = [c for c in self.rules["forbidden_characters"] if c in text]
        if forbidden:
            self.issue(child, "forbidden-character", "Character & not allowed",
                       f"{label} contains \"&\", which is not allowed anywhere in the FCS XML import file (p.16).",
                       page=16, path=path, field=label)
        if not text.strip():
            if element.required:
                self.issue(child, "required-blank", f"Required {label} is blank",
                           f"{label} is required for import, but it is empty.", page=element.page, path=path, field=label)
            elif element.type not in ("String", None):
                self.issue(child, "empty-value", f"{label} is empty",
                           f"{label} is a {element.type} and cannot be empty; leave the element out instead.",
                           page=element.page, path=path, field=label)
            return
        for p in check_value(element, text):
            self.issue(child, p.code, p.summary, f"{label}: {p.reason}", p.severity, element.page, path, label)
        if label == "WorkSet.WorkSetID":
            rule = self.spec.rules["workset_id"]
            bad = sorted(set(text) & set(rule["forbidden"]))
            if bad:
                shown = ", ".join("space" if c == " " else f"'{c}'" for c in bad)
                self.issue(child, "route-id-characters", "WorkSet.WorkSetID: characters not allowed",
                           f"A route ID cannot contain a space or any of \\ / : * ? \" < > | & ' [ = (p.54), but it "
                           f"contains {shown}.", page=54, path=path, field=label)

    def session_input(self, el):
        """One input type per meter (p.51); ReadMethod must match it (p.3, p.51)."""
        ns = self.rules["namespace"]
        present = [_local(c.tag) for c in self.children(el) if _local(c.tag) in self.rules["single_input"]]
        optical = el.find(f"{{{ns}}}MeterSessionInputOptical")
        if optical is not None and optical.find(f"{{{ns}}}MeterSessionInputOpRd") is not None:
            present.append("MeterSessionInputOpRd")
        kinds = sorted(set(present) - ({"MeterSessionInputOptical"} if "MeterSessionInputOpRd" in present else set()))
        if len(kinds) > 1:
            self.issue(el, "multiple-inputs", "More than one input type",
                       f"<MeterSessionInput> contains {', '.join(kinds)}. A meter can have only one type of input; "
                       f"with more than one, the meter information does not load to the handheld (p.51).",
                       path=self.path_of(el), page=51)
        method = (el.findtext(f"{{{ns}}}ReadMethod") or "0").strip()
        prompt = (el.findtext(f"{{{ns}}}PromptCode") or "").strip()
        if method == "5" and prompt != "1":
            self.issue(el, "read-method-5-prompt", "ReadMethod 5 without PromptCode 1",
                       f"ReadMethod 5 (No Read) is only valid with PromptCode 1, but the PromptCode is "
                       f"\"{prompt or 'blank'}\" (p.78).", path=self.path_of(el), page=78)
        wanted = self.spec.rules["read_method_children"].get(method)
        has = set(present) | ({"MeterSessionInputOptical"} if "MeterSessionInputOpRd" in present else set())
        if wanted and wanted not in has:
            self.issue(el, "read-method-missing-data", f"ReadMethod {method} without {wanted}",
                       f"ReadMethod is {method}, which needs a <{wanted}> entity, but there is none. FCS changes the "
                       f"ReadMethod to 0 (manual) on import (p.3).", path=self.path_of(el), page=3)
        for other in sorted(has - {wanted}):
            if other == "MeterSessionInputOpRd":
                continue
            codes = "/".join(k for k, v in self.spec.rules["read_method_children"].items() if v == other)
            self.issue(el, "read-method-unexpected-data", f"{other} but ReadMethod is not {codes}",
                       f"<{other}> is included, but ReadMethod is {method}. Its data is only used when the ReadMethod "
                       f"is {codes} (p.3).", path=self.path_of(el), page=3)

    def start_account(self, cust):
        ns = self.rules["namespace"]
        name = (cust.findtext(f"{{{ns}}}FullName") or "").strip()
        acct = (cust.findtext(f"{{{ns}}}AccountNumber") or "").strip()
        ws = cust
        while ws is not None and _local(ws.tag) != "WorkSet":
            ws = ws.getparent()
        route = (ws.findtext(f"{{{ns}}}WorkSetID") or "").strip() if ws is not None else ""
        idx = len(self.grouping.accounts)
        who = name or "Unnamed customer"
        label = f"account # {acct}" if acct else "no account number"
        self.grouping.accounts.append((f"{who} — {label}, route {route or '(unknown)'}, starting on line "
                                       f"{cust.sourceline}", cust.sourceline))
        self.own(cust, ("account", idx))


def validate_xml(content: bytes) -> tuple[list[Issue], Grouping]:
    spec = load_fcs()
    checker = XmlChecker(spec)
    checker.grouping.line_count = content.count(b"\n") + (0 if content.endswith(b"\n") else 1) if content else 0
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False,
                             remove_comments=True, remove_pis=True)
    try:
        root = etree.fromstring(content, parser)
    except etree.XMLSyntaxError as e:
        line = e.position[0] if e.position else None
        if content.lstrip()[:1] not in (b"<", b"\xef", b"\xff", b"\xfe"):
            msg = "This file does not look like XML. Choose the FCS CSV Import format for CSV files."
        else:
            msg = (f"The file is not well-formed XML: {e.msg}. Every element needs a matching end tag, tags are "
                   f"case-sensitive, and & must not appear in the file (p.10, p.16). Nothing after this point was checked.")
        checker.issues.append(Issue(severity=Severity.ERROR, code="xml-syntax", summary="XML is not well-formed",
                                    line=line, message=msg, page=10))
        return checker.issues, checker.grouping
    checker.run(root)
    return checker.issues, checker.grouping
