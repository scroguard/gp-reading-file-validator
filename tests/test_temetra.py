"""Temetra CSV and XML import validation. All fixtures are synthetic."""

import csv
import io

from test_fcs import xml_file

from rsvalidator.engine import validate
from rsvalidator.issues import Severity

HEADER = ["IGNORE", "CANCREATE", "CREF", "METERSERIAL", "METERUNITS", "METERNOMINALSIZE_IMPERIAL", "METERFORMAT",
          "METERTYPE", "METERMODEL", "COLLECTIONMETHOD", "MIUSERIAL", "METERINSTALLATIONDATE", "ACCOUNTREF",
          "ACCOUNTNAME", "ADDRESSLINE1", "ROUTENAME", "SEQUENCE", "GPS", "ADDTAG"]
ROW = {"IGNORE": "no", "CANCREATE": "yes", "CREF": "C001", "METERSERIAL": "1234567", "METERUNITS": "kWh",
       "METERNOMINALSIZE_IMPERIAL": '"5/8"""', "METERFORMAT": "5.0", "METERTYPE": "Generic", "METERMODEL": "Electricity",
       "COLLECTIONMETHOD": "Manual Read", "MIUSERIAL": "", "METERINSTALLATIONDATE": "31/12/2024", "ACCOUNTREF": "A100",
       "ACCOUNTNAME": "SMITH JOHN", "ADDRESSLINE1": "1 Main St", "ROUTENAME": "Route 1", "SEQUENCE": "10",
       "GPS": "N26.672412 W81.773412", "ADDTAG": "active-ind=A mcategory=0 readtype=01"}


def row(n=0, **changes):
    r = dict(ROW, CREF=f"C{n:03}", METERSERIAL=str(1234567 + n), ACCOUNTREF=f"A{100 + n}", SEQUENCE=str(10 * (n + 1)))
    r.update(changes)
    return [r[h] for h in HEADER]


def tcsv(rows=None, header=None) -> bytes:
    rows = [row()] if rows is None else rows
    return ("\r\n".join(",".join(r) for r in [header or HEADER] + rows) + "\r\n").encode()


def run(content, name="assets.csv", fmt="temetra-csv"):
    report = validate(content, name, fmt)
    return report, [i for s in report.sections for i in s.issues]


def codes(issues, severity=None):
    return sorted(i.code for i in issues if severity is None or i.severity == severity)


def test_valid_new_asset_file():
    report, issues = run(tcsv([row(0), row(1)]))
    assert issues == []
    assert report.account_count == 2


def test_ignored_rows_are_skipped():
    notes = ["yes", "These are my notes, not data"] + [""] * (len(HEADER) - 2)
    _, issues = run(tcsv([row(0), notes]))
    assert issues == []


def test_new_asset_must_start_with_ignore_cancreate():
    header = ["CANCREATE", "IGNORE"] + HEADER[2:]
    _, issues = run(tcsv([row(0)[1:2] + row(0)[0:1] + row(0)[2:]], header))
    assert codes(issues) == ["first-columns"]


def test_mandatory_columns_and_recommended():
    header = [h for h in HEADER if h not in ("METERFORMAT", "SEQUENCE")]
    rows = [[v for h, v in zip(HEADER, row(0)) if h in header]]
    _, issues = run(tcsv(rows, header))
    assert codes(issues, Severity.ERROR) == ["header-missing"]
    assert codes(issues, Severity.WARNING) == ["header-recommended"]


def test_unknown_heading_suggests_spelling_and_lowercase_is_error():
    header = HEADER + ["PERMANENTLLYDISCONNECTED", "enableaccount"]
    _, issues = run(tcsv([row(0) + ["FALSE", "true"]], header))
    unknown = next(i for i in issues if i.code == "header-unknown")
    assert "Did you mean PERMANENTLYDISCONNECTED?" in unknown.message
    assert codes(issues, Severity.ERROR) == ["header-case"]
    assert codes(issues, Severity.WARNING) == ["header-unknown"]  # Temetra ignores unknown columns


def test_heading_with_trailing_space_is_a_warning():
    header = [h + " " if h == "METERINSTALLATIONDATE" else h for h in HEADER]
    _, issues = run(tcsv([row(0)], header))
    assert codes(issues) == ["header-spaces"]


def test_field_values():
    bad = row(0, IGNORE="maybe", METERSERIAL="1234", METERINSTALLATIONDATE="12/31/2024", METERFORMAT="5.7",
              COLLECTIONMETHOD="Wireless")
    _, issues = run(tcsv([bad]))
    assert codes(issues) == ["invalid-date", "invalid-value", "invalid-value", "length", "miu-missing"]


def test_meter_format_accepts_whole_numbers():
    _, issues = run(tcsv([row(0, METERFORMAT="5")]))
    assert issues == []


def test_generic_meter_model():
    _, issues = run(tcsv([row(0, METERMODEL="AC-250")]))
    assert codes(issues) == ["generic-model"]


def test_duplicate_cref_must_be_fixed_and_a_corrected_file_is_offered():
    # A kWh/kW meter exported as two rows: the repeated CREF overwrote the kWh meter on import (confirmed).
    rows = [row(0), row(1, CREF="C000", METERSERIAL="1234567", METERUNITS="kW"),
            row(2, CREF="C000", METERSERIAL="1234567", METERUNITS="kW")]
    report, issues = run(tcsv(rows))
    dups = [i for i in issues if i.code == "duplicate-cref"]
    assert [d.severity for d in dups] == [Severity.ERROR, Severity.ERROR]
    assert dups[0].message.startswith("Duplicate CREF detected on line 3: CREF C000 is also used on line 2.")
    assert dups[0].fix.startswith("Corrected automatically in the corrected file: CREF C000 \u2192 C000-1;")

    fixed = report.corrected
    assert fixed.filename == "assets-corrected.csv" and fixed.count == 2 and fixed.errors_after == 0
    out = list(csv.reader(io.StringIO(fixed.content.decode())))
    h = out[0]
    get = lambda r, name: r[h.index(name)]
    assert [get(r, "CREF") for r in out[1:]] == ["C000", "C000-1", "C000-2"]
    assert [get(r, "METERSERIAL") for r in out[1:]] == ["1234567", "1234567-1", "1234567-2"]
    assert h[-1] == "LINKEDMETERSERIAL" and fixed.added_columns == ["LINKEDMETERSERIAL"]
    assert [get(r, "LINKEDMETERSERIAL") for r in out[1:]] == ["", "1234567", "1234567"]
    assert get(out[2], "ADDTAG").endswith("original-meter-serial=1234567")
    assert out[1] == next(csv.reader([",".join(row(0))])) + [""]  # uncorrected row: same values, empty new column

    # each correction is listed in the section of the account it belongs to
    noted = [(sec.title, c) for sec in report.sections for c in sec.corrections]
    assert [c.line for _, c in noted] == [3, 4]
    assert "account # A101" in noted[0][0] and "account # A102" in noted[1][0]
    assert "METERSERIAL 1234567 \u2192 1234567-1" in noted[0][1].summary


def test_different_meters_sharing_a_cref_are_not_corrected():
    _, issues = run(tcsv([row(0), row(1, CREF="C000")]))
    [dup] = [i for i in issues if i.code == "duplicate-cref"]
    assert "two different meters" in dup.fix


def test_corrected_file_keeps_other_rows_byte_for_byte():
    header = HEADER + ["LINKEDMETERSERIAL"]
    rows = [row(0) + [""], row(1, CREF="C000", METERSERIAL="1234567") + [""]]
    content = tcsv(rows, header).replace(b"SMITH JOHN", b"SMITH  JOHN")
    report, _ = run(content)
    before, after = content.split(b"\r\n"), report.corrected.content.split(b"\r\n")
    assert before[0] == after[0] and before[1] == after[1] and before[2] != after[2]
    assert report.corrected.added_columns == []


def test_tags():
    tags = "active-ind=X mcategory=0 cellular-device-installed=1 my-utility-tag readtype=G"
    _, issues = run(tcsv([row(0, ADDTAG=tags)]))
    assert codes(issues, Severity.ERROR) == ["tag-value", "tag-value", "tag-value"]
    assert codes(issues, Severity.WARNING) == ["tag-unknown"]


def test_gps_formats():
    rows = [row(0, GPS="N47 38.650140 W117 4.707960"), row(1, GPS="N26.6724 W81.7734"), row(2, GPS='"26.67 -81.77"')]
    _, issues = run(tcsv(rows))
    assert codes(issues, Severity.WARNING) == ["gps-precision"]
    assert codes(issues, Severity.ERROR) == ["invalid-gps"]


def test_blank_cref_in_new_asset_file():
    _, issues = run(tcsv([row(0, CREF="")]))
    assert codes(issues) == ["cref-blank"]


def test_meter_replacement_file_name_and_columns():
    header = ["METERSERIAL", "CREF", "REPLACEMENTMETERSERIAL", "FINALDECIMALINDEX"]
    content = tcsv([["OLD12345", "C1", "NEW12345", "1345"]], header)
    _, issues = run(content, name="replacements.csv")
    assert "filename" in codes(issues) and codes(issues).count("header-missing") == 8
    _, issues = run(content, name="meterreplacement-2025.csv")
    assert "filename" not in codes(issues)


def test_schedule_file():
    header = ["CREF", "METERSERIAL", "SCHEDULENAME", "FROMDATE", "TODATE", "METERTAGS"]
    rows = [["", "12345678", "April", "01/04/2025", "30/04/2025", "noformat-high-1=6666 prompt-code=Q"]]
    _, issues = run(tcsv(rows, header), name="new-schedules-from-existing-meters-april.csv")
    assert codes(issues) == ["tag-value"]


def test_historical_reads_file():
    header = ["CREF", "METERSERIAL", "READTIME", "DECIMALINDEX", "TYPE"]
    rows = [["001", "10VI0026171", "01/03/2017 10:22", "151.20", "Wireless"],
            ["002", "10VI0026172", "2017-03-01", "abc", "Guess"]]
    _, issues = run(tcsv(rows, header), name="history.csv")
    assert codes(issues) == ["invalid-date", "invalid-value", "not-number"]


def test_deschedule_needs_columns():
    _, issues = run(tcsv([["C1", "Spring"]], ["CREF", "SCHEDULENAME"]), name="deschedule-existing-1.csv")
    assert codes(issues) == ["header-missing"]


# --- XML (same format family as FCS; Temetra supports a subset) -----------------

def temetra_xml(**kw) -> bytes:
    return xml_file(**kw)  # the synthetic FCS import file only uses elements Temetra also supports


def test_temetra_xml_valid():
    report, issues = run(temetra_xml(), "import.xml", "temetra-xml")
    assert issues == [] and report.account_count == 1


def test_temetra_xml_rejects_fcs_only_codes_and_needs_ownership():
    codes_xml = """
  <Codes>
    <MeterTypeCodes ForceLoad="true"/>
  </Codes>"""
    content = temetra_xml(codes_xml=codes_xml)
    _, issues = run(content, "import.xml", "temetra-xml")
    assert codes(issues) == ["required-attribute", "unknown-element"]


def test_temetra_xml_duplicate_route_ids():
    content = temetra_xml()
    start, end = content.index(b"    <WorkSet>"), content.index(b"  </WorkSets>")
    content = content[:end] + content[start:end] + content[end:]
    _, issues = run(content, "import.xml", "temetra-xml")
    assert codes(issues) == ["duplicate-route"]


def test_temetra_dials_range_differs_from_fcs():
    content = temetra_xml().replace(b"<NumberDials>5</NumberDials>", b"<NumberDials>3</NumberDials>")
    _, fcs = run(content, "import.xml", "fcs-xml")
    _, tem = run(content, "import.xml", "temetra-xml")
    assert fcs == [] and codes(tem) == ["out-of-range"]


def test_padded_lookup_values_and_blank_required_are_warnings():
    _, issues = run(tcsv([row(0, METERTYPE="Generic     ", ROUTENAME=" ")]))
    assert codes(issues, Severity.WARNING) == ["blank-value", "padded-value"]
    assert codes(issues, Severity.ERROR) == []


def test_duplicate_serial_alone_is_a_warning():
    _, issues = run(tcsv([row(0), row(1, METERSERIAL="1234567")]))
    [dup] = issues
    assert dup.code == "duplicate-value" and dup.severity == Severity.WARNING


def test_meter_units_are_not_case_sensitive():
    for units in ("KWH", "kwh", "Gal", "CU FT"):
        _, issues = run(tcsv([row(0, METERUNITS=units)]))
        assert issues == [], units
    _, issues = run(tcsv([row(0, METERUNITS="litres")]))
    assert codes(issues, Severity.WARNING) == ["value-not-listed"]
