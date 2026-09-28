"""MV-RS Host Download: record hierarchy and cross-record rules.

Field-level checks come from the layout in formats/mvrs.yaml (see fields.py).
This module builds the file -> cycle -> route -> account -> meter -> reading
tree (guide p.2 layout diagram) and applies the rules that span records.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .issues import Issue, Severity
from .lines import Line
from .spec import FormatSpec, RecordSpec

RECORD_NAMES = {
    "FHD": "File Header", "FTR": "File Trailer",
    "CHD": "Cycle Header", "CTR": "Cycle Trailer",
    "RHD": "Standard Route Header", "RTR": "Standard Route Trailer",
    "ERH": "Extended Route Header", "ERT": "Extended Route Trailer",
    "RTS": "Route Header Survey", "RTM": "Route Header Message",
    "CUS": "Customer", "CSX": "Customer Extra",
    "MTR": "Meter", "MTX": "Meter Extra", "MTS": "Meter Special Text", "PRB": "Optical Probe",
    "RDG": "Reading", "RFF": "Off-site (Radio) Read", "WRR": "Remote (Wand) Read",
}
TABLE_IDS = ("FC", "ML", "MT", "RI", "RT", "SC", "TC")
READ_METHOD_NAMES = {"K": "Keyed", "N": "No Read", "P": "Probed optically", "R": "Radio", "W": "Wand"}
ROUTE_TRAILER_FOR = {"RHD": "RTR", "ERH": "ERT"}
ROUTE_TOTALS_TRAILER_ONLY = {"total_reading_records", "total_customer_records", "total_meter_records"}


def rname(rid: str) -> str:
    name = RECORD_NAMES.get(rid, "Table" if rid in TABLE_IDS else None)
    return f"{name} ({rid})" if name else rid


@dataclass
class Rec:
    line: Line
    rid: str
    spec: RecordSpec

    @property
    def n(self) -> int:
        return self.line.number

    def raw(self, name: str) -> str:
        return self.spec.field(name).slice(self.line.data).ljust(self.spec.field(name).length)

    def val(self, name: str) -> str:
        return self.raw(name).strip()


@dataclass
class Reading:
    rdg: Rec
    rff: Rec | None = None
    wrr: Rec | None = None


@dataclass
class Meter:
    mtr: Rec
    mtx: Rec | None = None
    mts: Rec | None = None
    prb: Rec | None = None
    readings: list[Reading] = field(default_factory=list)


@dataclass
class Account:
    index: int
    cus: Rec
    csx: Rec | None = None
    meters: list[Meter] = field(default_factory=list)
    last_line: int = 0

    @property
    def name(self) -> str:
        return self.cus.val("name")

    @property
    def account_number(self) -> str:
        return self.cus.val("account_number")


@dataclass
class Route:
    index: int
    header: Rec | None
    trailer: Rec | None = None
    rts: Rec | None = None
    rtm: Rec | None = None
    accounts: list[Account] = field(default_factory=list)
    first_line: int = 0

    @property
    def number(self) -> str:
        rec = self.header or self.trailer
        return rec.val("route_number") if rec else ""


@dataclass
class Cycle:
    header: Rec | None
    trailer: Rec | None = None
    routes: list[Route] = field(default_factory=list)


@dataclass
class FileTree:
    fhd: Rec | None = None
    ftr: Rec | None = None
    tables: list[Rec] = field(default_factory=list)
    cycles: list[Cycle] = field(default_factory=list)
    accounts: list[Account] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    # line number -> ("account", index) | ("route", index) | ("file", 0)
    owner: dict[int, tuple[str, int]] = field(default_factory=dict)


def _issue(code, summary, message, rec: Rec | Line | None = None, severity=Severity.ERROR,
           start=None, end=None, field_label=None, page=None) -> Issue:
    line = rec.line if isinstance(rec, Rec) else rec
    return Issue(
        severity=severity, code=code, summary=summary, message=message,
        line=line.number if line else None,
        record=rec.rid if isinstance(rec, Rec) else (line.data[:3] if line else None),
        start=start, end=end, field=field_label, page=page,
    )


def _field_issue(code, summary, rec: Rec, fname: str, reason: str, severity=Severity.ERROR, page=None) -> Issue:
    f = rec.spec.field(fname)
    msg = (f"The {rec.rid} record on line {rec.n} has a problem in {f.bytes_label()} ({f.label}). {reason}")
    return _issue(code, summary, msg, rec, severity, f.start, f.end, f.label, page or f.page)


class TreeBuilder:
    """Places each record in the hierarchy, reporting records that are out of order."""

    def __init__(self, spec: FormatSpec):
        self.spec = spec
        self.tree = FileTree()
        self.issues: list[Issue] = []
        self.cycle: Cycle | None = None
        self.route: Route | None = None
        self.account: Account | None = None
        self.meter: Meter | None = None
        self.reading: Reading | None = None
        self.first_line: int | None = None
        self.after_ftr: bool = False

    def misplaced(self, rec: Rec, why: str):
        self.issues.append(_issue(
            "record-out-of-place", "Record out of place",
            f"The {rname(rec.rid)} record on line {rec.n} is out of place: {why}", rec, page=2))

    def own(self, line: int):
        if self.account:
            self.tree.owner[line] = ("account", self.account.index)
            self.account.last_line = line
        elif self.route:
            self.tree.owner[line] = ("route", self.route.index)
        else:
            self.tree.owner[line] = ("file", 0)

    def close_route(self, at_line: int, reason: str):
        if self.route and self.route.trailer is None:
            rid = self.route.header.rid if self.route.header else "RHD"
            self.issues.append(_issue(
                "missing-trailer", "Missing trailer record",
                f"The route {self.route.number or '(unknown)'} starting on line {self.route.first_line} has no "
                f"{rname(ROUTE_TRAILER_FOR.get(rid, 'RTR'))} record {reason} line {at_line}. Every route must end "
                f"with a route trailer.", None, page=2))
            self.tree.owner.setdefault(self.route.first_line, ("route", self.route.index))
        self.route = self.account = self.meter = self.reading = None

    def close_cycle(self, at_line: int, reason: str):
        self.close_route(at_line, reason)
        if self.cycle and self.cycle.trailer is None and self.cycle.header:
            self.issues.append(_issue(
                "missing-trailer", "Missing trailer record",
                f"The cycle starting on line {self.cycle.header.n} has no Cycle Trailer (CTR) record {reason} "
                f"line {at_line}. Every cycle must end with a cycle trailer.", None, page=9))
        self.cycle = None

    def ensure_cycle(self, rec: Rec) -> Cycle:
        if self.cycle is None:
            self.misplaced(rec, "it is not inside a cycle. Route records must follow a Cycle Header (CHD).")
            self.cycle = Cycle(header=None)
            self.tree.cycles.append(self.cycle)
        return self.cycle

    def ensure_route(self, rec: Rec) -> Route:
        if self.route is None:
            self.misplaced(rec, "it is not inside a route. It must follow a route header (RHD or ERH).")
            cycle = self.ensure_cycle(rec)
            self.route = Route(index=len(self.tree.routes), header=None, first_line=rec.n)
            self.tree.routes.append(self.route)
            cycle.routes.append(self.route)
        return self.route

    def add(self, line: Line, rid: str, rspec: RecordSpec):
        rec = Rec(line, rid, rspec)
        if self.first_line is None:
            self.first_line = line.number
        if self.after_ftr and rid != "FTR":
            self.misplaced(rec, "it comes after the File Trailer (FTR), which must be the last line in the file.")

        handler = getattr(self, f"on_{rid}", None) or (self.on_table if rid in TABLE_IDS else None)
        handler(rec)
        self.own(line.number)

    def add_unknown(self, line: Line):
        self.own(line.number)

    # --- handlers -----------------------------------------------------------

    def on_FHD(self, rec: Rec):
        if self.tree.fhd is not None:
            self.misplaced(rec, f"the file already has a File Header on line {self.tree.fhd.n}.")
            return
        if rec.n != self.first_line:
            self.misplaced(rec, "the File Header must be the first line in the file.")
        self.tree.fhd = rec

    def on_table(self, rec: Rec):
        if self.tree.cycles:
            self.misplaced(rec, "Table records must come after the File Header and before the first Cycle Header.")
        self.tree.tables.append(rec)

    def on_CHD(self, rec: Rec):
        self.close_cycle(rec.n, "before the next Cycle Header on")
        self.cycle = Cycle(header=rec)
        self.tree.cycles.append(self.cycle)

    def on_CTR(self, rec: Rec):
        if self.cycle is None or self.cycle.trailer is not None:
            self.misplaced(rec, "there is no open cycle for it to close.")
            return
        self.close_route(rec.n, "before the Cycle Trailer on")
        self.cycle.trailer = rec
        self.cycle = None

    def on_route_header(self, rec: Rec):
        self.close_route(rec.n, "before the next route header on")
        cycle = self.ensure_cycle(rec)
        self.route = Route(index=len(self.tree.routes), header=rec, first_line=rec.n)
        self.tree.routes.append(self.route)
        cycle.routes.append(self.route)

    on_RHD = on_ERH = on_route_header

    def on_route_trailer(self, rec: Rec):
        if self.route is None:
            self.misplaced(rec, "there is no open route for it to close.")
            return
        header = self.route.header
        if header is not None and ROUTE_TRAILER_FOR[header.rid] != rec.rid:
            self.misplaced(rec, (
                f"the route was opened with a {rname(header.rid)} on line {header.n}, so it must be closed with a "
                f"{rname(ROUTE_TRAILER_FOR[header.rid])}. Standard and extended route records cannot be mixed (p.9)."))
        self.route.trailer = rec
        self.account = None
        self.own(rec.n)
        self.route = self.meter = self.reading = None

    on_RTR = on_ERT = on_route_trailer

    def on_RTS(self, rec: Rec):
        route = self.ensure_route(rec)
        if route.accounts or route.rtm or route.rts:
            self.misplaced(rec, "the Route Header Survey (RTS) record must directly follow the route header, "
                                "before any Route Header Message (RTM) or Customer records (p.13).")
        route.rts = route.rts or rec

    def on_RTM(self, rec: Rec):
        route = self.ensure_route(rec)
        if route.accounts or route.rtm:
            self.misplaced(rec, "the Route Header Message (RTM) record must follow the route header or survey "
                                "record, before any Customer records (p.13).")
        route.rtm = route.rtm or rec

    def on_CUS(self, rec: Rec):
        route = self.ensure_route(rec)
        self.account = Account(index=len(self.tree.accounts), cus=rec, last_line=rec.n)
        self.tree.accounts.append(self.account)
        route.accounts.append(self.account)
        self.meter = self.reading = None

    def on_CSX(self, rec: Rec):
        if self.account is None or self.account.meters or self.account.csx:
            self.misplaced(rec, "the Customer Extra (CSX) record must immediately follow its Customer (CUS) record (p.15).")
            if self.account is None:
                return
        self.account.csx = self.account.csx or rec

    def on_MTR(self, rec: Rec):
        if self.account is None:
            self.misplaced(rec, "a Meter (MTR) record must belong to a Customer (CUS) record.")
            self.ensure_route(rec)
            return
        self.meter = Meter(mtr=rec)
        self.account.meters.append(self.meter)
        self.reading = None

    def _meter_child(self, rec: Rec, attr: str, after: tuple[str, ...], rule: str):
        m = self.meter
        if m is None or m.readings or getattr(m, attr) or any(getattr(m, a) for a in after):
            self.misplaced(rec, rule)
            if m is None:
                return
        if getattr(m, attr) is None:
            setattr(m, attr, rec)

    def on_MTX(self, rec: Rec):
        self._meter_child(rec, "mtx", ("mts", "prb"),
                          "the Meter Extra (MTX) record must follow its Meter (MTR) record, before any MTS, PRB or Reading records (p.6, p.18).")

    def on_MTS(self, rec: Rec):
        self._meter_child(rec, "mts", ("prb",),
                          "the Meter Special Text (MTS) record must follow its Meter (MTR) record (and MTX, if present), before any PRB or Reading records (p.6, p.18).")

    def on_PRB(self, rec: Rec):
        self._meter_child(rec, "prb", (),
                          "the Optical Probe (PRB) record must follow its Meter record and come before the meter's Reading records (p.2).")

    def on_RDG(self, rec: Rec):
        if self.meter is None:
            self.misplaced(rec, "a Reading (RDG) record must belong to a Meter (MTR) record.")
            return
        self.reading = Reading(rdg=rec)
        self.meter.readings.append(self.reading)

    def _reading_child(self, rec: Rec, attr: str):
        if self.reading is None or getattr(self.reading, attr):
            self.misplaced(rec, f"the {rname(rec.rid)} record must directly follow the Reading (RDG) record it belongs to, once (p.2).")
            if self.reading is None:
                return
        if getattr(self.reading, attr) is None:
            setattr(self.reading, attr, rec)

    def on_RFF(self, rec: Rec):
        self._reading_child(rec, "rff")

    def on_WRR(self, rec: Rec):
        self._reading_child(rec, "wrr")

    def on_FTR(self, rec: Rec):
        if self.tree.ftr is not None:
            self.misplaced(rec, f"the file already has a File Trailer on line {self.tree.ftr.n}.")
            return
        self.close_cycle(rec.n, "before the File Trailer on")
        self.tree.ftr = rec
        self.after_ftr = True

    def finish(self, last_line: int):
        self.close_cycle(last_line, "before the end of the file at")
        if self.tree.fhd is None:
            self.issues.append(_issue("missing-record", "Missing required record",
                                      "The file has no File Header (FHD) record. It must be the first line in the file.",
                                      None, page=7))
        if self.tree.ftr is None:
            self.issues.append(_issue("missing-record", "Missing required record",
                                      "The file has no File Trailer (FTR) record. It must be the last line in the file.",
                                      None, page=7))
        if not self.tree.cycles:
            self.issues.append(_issue("missing-record", "Missing required record",
                                      "The file contains no cycles. At least one Cycle Header (CHD) and Cycle Trailer (CTR) is required.",
                                      None, page=9))
        for cycle in self.tree.cycles:
            if cycle.header and not cycle.routes:
                self.issues.append(_issue("missing-record", "Missing required record",
                                          f"The cycle starting on line {cycle.header.n} contains no routes.", cycle.header, page=9))
        for route in self.tree.routes:
            if not route.accounts:
                self.issues.append(_issue("missing-record", "Missing required record",
                                          f"The route {route.number} starting on line {route.first_line} contains no Customer (CUS) records.",
                                          route.header, page=98))
        for acct in self.tree.accounts:
            if not acct.meters:
                self.issues.append(_issue("missing-record", "Missing required record",
                                          f"The Customer (CUS) record on line {acct.cus.n} has no Meter (MTR) records. "
                                          f"There must be at least one Meter record for each Customer record (p.15).",
                                          acct.cus, page=15))
            for m in acct.meters:
                if not m.readings:
                    self.issues.append(_issue("missing-record", "Missing required record",
                                              f"The Meter (MTR) record on line {m.mtr.n} has no Reading (RDG) records. "
                                              f"At least one Reading record is required for each Meter record (p.20).",
                                              m.mtr, page=20))


class RuleChecker:
    """Rules that relate one record to another."""

    def __init__(self, spec: FormatSpec, tree: FileTree):
        self.spec = spec
        self.tree = tree
        self.rules = spec.rules
        self.issues: list[Issue] = []

    def run(self) -> list[Issue]:
        self.header_trailer_pairs()
        self.file_counts()
        self.file_indicators()
        for route in self.tree.routes:
            self.route_rules(route)
        return self.issues

    def add(self, issue: Issue):
        self.issues.append(issue)

    # --- header/trailer ----------------------------------------------------

    def compare_pair(self, header: Rec | None, trailer: Rec | None, skip: set[str], page: int):
        if header is None or trailer is None:
            return
        for f in header.spec.fields:
            if f.pad or f.name in skip:
                continue
            h, t = header.raw(f.name), trailer.raw(f.name)
            if h != t:
                self.add(_field_issue(
                    "header-trailer-mismatch", f"{rname(trailer.rid)} does not match header", trailer, f.name,
                    f"It should match the {rname(header.rid)} on line {header.n}, but the header contains "
                    f"\"{h.rstrip()}\" and the trailer contains \"{t.rstrip()}\". The guide says header and trailer "
                    f"records contain identical information (p.{page}); FCS accepts a mismatch, so this is a warning.",
                    severity=Severity.WARNING, page=page))

    def header_trailer_pairs(self):
        t = self.tree
        fhd_cycles = t.fhd.val("number_of_cycles") if t.fhd else ""
        # Number of Cycles is only required in the trailer (p.69); compare it when the header has one.
        self.compare_pair(t.fhd, t.ftr, set() if fhd_cycles else {"number_of_cycles"}, 7)
        for c in t.cycles:
            self.compare_pair(c.header, c.trailer, set(), 9)
        for r in t.routes:
            self.compare_pair(r.header, r.trailer, ROUTE_TOTALS_TRAILER_ONLY, 9)

    # --- counts ------------------------------------------------------------

    def count_check(self, rec: Rec | None, fname: str, actual: int, what: str, page=None, warning: str | None = None):
        """Compare a count field with the file. `warning` (the reason) makes a mismatch a warning."""
        if rec is None:
            return
        raw = rec.val(fname)
        if not raw.isdigit():
            return  # field-level check reports it
        if int(raw) != actual:
            label = rec.spec.field(fname).label
            self.add(_field_issue(
                "total-mismatch" if warning else "count-mismatch",
                f"{label} does not match file contents",
                rec, fname,
                f"It says {int(raw)}, but " + what.format(actual=actual) + "." + (f" {warning}" if warning else ""),
                severity=Severity.WARNING if warning else Severity.ERROR, page=page))

    def file_counts(self):
        t = self.tree
        n_cycles = len(t.cycles)
        continues = "MV-RS logs a message and imports everything, so this is a warning."
        self.count_check(t.ftr, "number_of_cycles", n_cycles, "the file contains {actual} cycle(s)", page=69, warning=continues)
        if t.fhd and t.fhd.val("number_of_cycles"):
            self.count_check(t.fhd, "number_of_cycles", n_cycles, "the file contains {actual} cycle(s)", page=69, warning=continues)
        for c in t.cycles:
            n = len(c.routes)
            self.count_check(c.header, "number_of_routes", n, "the cycle contains {actual} route(s)", page=70, warning=continues)
            self.count_check(c.trailer, "number_of_routes", n, "the cycle contains {actual} route(s)", page=70, warning=continues)
            cycle_id = c.header.val("cycle_id") if c.header else None
            for r in c.routes:
                rec = r.header or r.trailer
                if cycle_id and rec and r.number[:2] != cycle_id:
                    self.add(_field_issue(
                        "route-cycle-mismatch", "Route Number does not start with the cycle number", rec, "route_number",
                        f"The first two characters of the Route Number must be the cycle number (p.79). This route is in "
                        f"cycle {cycle_id} (line {c.header.n}), but the route number is \"{r.number}\".", page=79))

    # --- file indicators ---------------------------------------------------

    def file_indicators(self):
        t = self.tree
        if t.fhd is None:
            return
        present: dict[str, list[Rec]] = {}
        for rec in self._all_records():
            present.setdefault(rec.rid, []).append(rec)
        for rule in self.rules.get("file_indicators", []):
            flag = t.fhd.val(rule["field"])
            found = [r for rid in rule["records"] for r in present.get(rid, [])]
            label = t.fhd.spec.field(rule["field"]).label
            ids = "/".join(rule["records"]) if len(rule["records"]) <= 2 else "Table"
            if found and flag != "Y":
                first = min(found, key=lambda r: r.n)
                self.add(_field_issue(
                    "file-indicator", f"{label} does not match file contents", t.fhd, rule["field"],
                    f"It is \"{flag or 'blank'}\", but the file contains {len(found)} {ids} record(s), the first on line "
                    f"{first.n}. It must be Y when these records are present.", page=rule.get("page")))
            elif not found and flag == "Y":
                self.add(_field_issue(
                    "file-indicator-unused", f"{label} set but no records", t.fhd, rule["field"],
                    f"It is Y, but the file contains no {ids} records. MV-RS continues the import, so this is a warning.",
                    severity=Severity.WARNING, page=rule.get("page")))

    def _all_records(self):
        t = self.tree
        yield from t.tables
        for r in t.routes:
            for x in (r.header, r.trailer, r.rts, r.rtm):
                if x:
                    yield x
            for a in r.accounts:
                yield from self._account_records(a)

    @staticmethod
    def _account_records(a: Account):
        yield a.cus
        if a.csx:
            yield a.csx
        for m in a.meters:
            for x in (m.mtr, m.mtx, m.mts, m.prb):
                if x:
                    yield x
            for rd in m.readings:
                for x in (rd.rdg, rd.rff, rd.wrr):
                    if x:
                        yield x

    # --- route-level rules -------------------------------------------------

    def child_indicator(self, parent: Rec, fname: str, child: Rec | None, child_id: str, page: int):
        flag = parent.val(fname)
        label = parent.spec.field(fname).label
        if flag == "Y" and child is None:
            self.add(_field_issue(
                "indicator-missing-record", f"{label} is Y but no {child_id} record", parent, fname,
                f"It is Y, which means a {rname(child_id)} record must follow, but none was found.", page=page))
        elif child is not None and flag != "Y":
            self.add(_issue(
                "indicator-unexpected-record", f"{child_id} record present but {label} is not Y",
                f"The {rname(child_id)} record on line {child.n} is included, but the {label} in "
                f"{parent.spec.field(fname).bytes_label()} of the {parent.rid} record on line {parent.n} is "
                f"\"{flag or 'blank'}\". It must be Y when this record is included.",
                child, page=page))

    def route_rules(self, route: Route):
        header = route.header
        rules = self.rules.get("child_indicators", [])

        def indicator_rules(parent: Rec, children: dict[str, Rec | None]):
            for rule in rules:
                if parent.rid in rule["parent"] and rule["child"] in children:
                    self.child_indicator(parent, rule["field"], children[rule["child"]], rule["child"], rule["page"])

        if header:
            indicator_rules(header, {"RTS": route.rts, "RTM": route.rtm})
            if header.val("mobile_amr") == "M" and header.val("route_message_indicator") != "Y":
                self.add(_field_issue(
                    "mobile-amr-needs-rtm", "Mobile AMR route without Route Message", header, "route_message_indicator",
                    "This is a Mobile AMR route (Mobile AMR = M), and all Mobile AMR routes require a Route Message "
                    "(RTM) record, so the Route Message Indicator must be Y (p.80).", page=80))

        # Route number repeated in every record must match the route header (p.79).
        if header:
            number = header.raw("route_number")
            others = [x for x in (route.trailer, route.rts, route.rtm) if x]
            for a in route.accounts:
                others.extend(self._account_records(a))
            for rec in others:
                if rec.raw("route_number") != number:
                    self.add(_field_issue(
                        "route-number-mismatch", "Route Number does not match route header", rec, "route_number",
                        f"It must match the route header on line {header.n} (\"{number.rstrip()}\"), but it is "
                        f"\"{rec.raw('route_number').rstrip()}\".", page=79))

        totals = dict(readings=0, customers=len(route.accounts), meters=0, K=0, P=0, R=0, W=0,
                      G=0, E=0, Wm=0, L=0, X=0)
        last_seq: tuple[int, Rec] | None = None
        seen_seq: dict[int, Rec] = {}
        for a in route.accounts:
            indicator_rules(a.cus, {"CSX": a.csx})
            self.count_check(a.cus, "number_of_meters", len(a.meters), "{actual} Meter (MTR) record(s) follow before the next Customer record (it must match exactly, p.70)", page=70)
            if a.cus.val("segment_code") and set(a.cus.val("segment_code")) == {"0"}:
                # FCS imports this without complaint (confirmed by the user), but it may cause problems in Temetra.
                self.add(_field_issue("segment-all-zeros", "Segment Code is all zeros", a.cus, "segment_code",
                                      "The guide says the Segment Code cannot be all zeros (p.80). FCS accepts it, but "
                                      "it may cause problems when the file is imported into Temetra.",
                                      severity=Severity.RECOMMENDED, page=80))
            for m in a.meters:
                totals["meters"] += 1
                mtr = m.mtr
                indicator_rules(mtr, {"MTX": m.mtx, "MTS": m.mts})
                self.count_check(mtr, "number_of_readings", len(m.readings), "{actual} Reading (RDG) record(s) follow before the next Meter record (it must match exactly, p.70)", page=70)
                cat = mtr.val("meter_category")
                totals.update({k: totals[k] + 1 for k, c in (("G", "G"), ("E", "E"), ("Wm", "W")) if cat == c})
                le = mtr.val("location_extra_meter")
                if le == "L":
                    totals["L"] += 1
                elif le == "E":
                    totals["X"] += 1
                if mtr.val("special_message_indicator") == "Y" and mtr.val("special_message_display") not in ("1", "2", "3"):
                    self.add(_field_issue(
                        "special-message-display", "Special Message Display Indicator required", mtr, "special_message_display",
                        f"The Special Message Indicator is Y, so this must be 1, 2 or 3 (p.80), but it is "
                        f"\"{mtr.val('special_message_display') or 'blank'}\".", page=80))
                for child in (m.mtx, m.mts):
                    if child and child.raw("meter_number") != mtr.raw("meter_number"):
                        self.add(_field_issue(
                            "meter-number-mismatch", f"{child.rid} Meter Number does not match meter", child, "meter_number",
                            f"It must be the same meter number sent in the Meter record on line {mtr.n} "
                            f"(\"{mtr.val('meter_number')}\"), but it is \"{child.val('meter_number')}\" (p.18).", page=18))
                if m.prb and m.prb.raw("optical_probe_recorder_id") != mtr.raw("optical_probe_recorder_id"):
                    self.add(_field_issue(
                        "recorder-id-mismatch", "PRB Recorder ID does not match meter", m.prb, "optical_probe_recorder_id",
                        f"It must match the Optical Probe Recorder ID in the Meter record on line {mtr.n} "
                        f"(\"{mtr.val('optical_probe_recorder_id')}\"), but it is \"{m.prb.val('optical_probe_recorder_id')}\" (p.71).",
                        page=71))

                seq_raw = mtr.val("meter_read_sequence")
                if seq_raw.isdigit():
                    seq = int(seq_raw)
                    if seq == 0:
                        self.add(_field_issue("read-sequence-zero", "Meter Read Sequence is 0", mtr, "meter_read_sequence",
                                              "A sequence number of 0 is not recommended (p.64).",
                                              severity=Severity.WARNING, page=64))
                    if last_seq and seq < last_seq[0]:
                        self.add(_field_issue("read-sequence-decreases", "Meter Read Sequence decreases", mtr, "meter_read_sequence",
                                              f"Sequence numbers should never decrease from the start to the end of the route "
                                              f"(p.64), but {seq} follows {last_seq[0]} on line {last_seq[1].n}. MV-RS "
                                              f"corrects the sequence on import and warns the user, so this is a warning.",
                                              severity=Severity.WARNING, page=64))
                    elif seq in seen_seq:
                        self.add(_field_issue("read-sequence-duplicate", "Meter Read Sequence not unique", mtr, "meter_read_sequence",
                                              f"Sequence numbers should be unique within the route (p.64), but {seq} is also "
                                              f"used on line {seen_seq[seq].n}. MV-RS assigns a new unique number and logs "
                                              f"a warning, so this is a warning.", severity=Severity.WARNING, page=64))
                    seen_seq.setdefault(seq, mtr)
                    last_seq = (seq, mtr)

                self.read_method_rules(m, totals)

        self.route_totals(route, totals)

    def read_method_rules(self, m: Meter, totals: dict):
        mapping = self.rules.get("read_method_records", {})
        probed = []
        for rd in m.readings:
            totals["readings"] += 1
            method = rd.rdg.val("read_method")
            if method in ("K", "P", "R", "W"):
                totals[method] += 1
            if method == "P":
                probed.append(rd.rdg)
            if method == "N" and rd.rdg.val("prompt_code") not in ("P", "N"):
                self.add(_field_issue(
                    "read-method-n", "Read Method N without Prompt Code P or N", rd.rdg, "read_method",
                    f"Read Method N (No Read) is only used with a Prompt Code of P or N (p.21), but the Prompt Code "
                    f"is \"{rd.rdg.val('prompt_code') or 'blank'}\".", page=21))
            for rid, attr in (("RFF", "rff"), ("WRR", "wrr")):
                child = getattr(rd, attr)
                wanted = mapping.get(method) == rid
                page = 24 if rid == "RFF" else 23
                code = next(k for k, v in mapping.items() if v == rid)
                if wanted and child is None:
                    self.add(_field_issue(
                        "read-method-missing-record", f"Read Method {method} but no {rid} record", rd.rdg, "read_method",
                        f"The Read Method is {method} ({READ_METHOD_NAMES[method]}), which requires a {rname(rid)} record "
                        f"directly after this Reading record, but none was found. (MV-RS changes the read method to "
                        f"Keyed when it is missing.)", page=page))
                elif child is not None and not wanted:
                    self.add(_issue(
                        "read-method-unexpected-record", f"{rid} record present but Read Method is not {code}",
                        f"The {rname(rid)} record on line {child.n} is included, but the Read Method in byte 31 of the "
                        f"Reading (RDG) record on line {rd.rdg.n} is \"{method or 'blank'}\""
                        f"{' (' + READ_METHOD_NAMES[method] + ')' if method in READ_METHOD_NAMES else ''}. "
                        f"{rid} records are only included when the Read Method is {code} ({READ_METHOD_NAMES[code]}).",
                        child, page=page))
        if probed and m.prb is None:
            self.add(_field_issue(
                "read-method-missing-record", "Read Method P but no PRB record", probed[0], "read_method",
                f"The Read Method is P (Probed optically), which requires an Optical Probe (PRB) record after the "
                f"Meter record on line {m.mtr.n}, but none was found. (MV-RS changes the read method to Keyed when "
                f"it is missing.)", page=19))
        elif m.prb is not None and not probed:
            self.add(_issue(
                "read-method-unexpected-record", "PRB record present but no Read Method P",
                f"The Optical Probe (PRB) record on line {m.prb.n} is included, but none of the meter's Reading "
                f"records has Read Method P (Probed optically). PRB records are only included for optically probed "
                f"readings (p.19).", m.prb, page=19))

    def route_totals(self, route: Route, totals: dict):
        trailer = route.trailer
        header = route.header
        if header and header.rid == "RHD" and totals["readings"] > 9999:
            self.add(_issue(
                "extended-route-required", "Route needs Extended Route Header",
                f"The route starting on line {header.n} has {totals['readings']} readings. Routes with more than "
                f"9999 readings must use the Extended Route Header/Trailer (ERH/ERT) records (p.9).",
                header, page=9))
        if trailer is None:
            return
        self.count_check(trailer, "total_reading_records", totals["readings"], "the route contains {actual} Reading (RDG) record(s)", page=98)
        self.count_check(trailer, "total_customer_records", totals["customers"], "the route contains {actual} Customer (CUS) record(s)", page=96)
        self.count_check(trailer, "total_meter_records", totals["meters"], "the route contains {actual} Meter (MTR) record(s)", page=97)
        for fname, key, what, page in (
            ("total_keyed_readings", "K", "Reading records with Read Method K", 97),
            ("total_optical_probe_readings", "P", "Reading records with Read Method P", 97),
            ("total_offsite_readings", "R", "Reading records with Read Method R", 97),
            ("total_wand_reads", "W", "Reading records with Read Method W", 98),
            ("total_gas_meters", "G", "Meter records with Meter Category G", 96),
            ("total_water_meters", "Wm", "Meter records with Meter Category W", 98),
            ("total_electric_meters", "E", "Meter records with Meter Category E", 96),
            ("total_location_meters", "L", "Meter records marked L (Location meter)", 97),
            ("total_extra_meters", "X", "Meter records marked E (Extra meter)", 96),
        ):
            self.count_check(trailer, fname, totals[key], "the route contains {actual} " + what, page=page,
                             warning="This total is informational in MV-RS, so it is reported as a warning.")
