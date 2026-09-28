"""FCS CSV and XML import validation. All fixtures are synthetic."""

import pytest

from rsvalidator.engine import validate
from rsvalidator.issues import Severity

# --- CSV ---------------------------------------------------------------------

HEADER = ["WorkSet.WorkSetID", "WorkSet.WorkSetType", "Customer.FullName", "Customer.AccountNumber",
          "Meter.MeterNumber", "Meter.MeterCategory", "MeterSessionInput.NumberDials",
          "MeterSessionInput.ReadMethod", "MeterSessionInput.PromptCode", "MeterSessionInputRF.EndpointID"]
ROW = ["ROUTE001", "5", "SMITH JOHN", "1001", "M1001", "0", "5", "0", "0", ""]


def csv_file(rows=None, header=None, sep="\r\n") -> bytes:
    rows = [ROW] if rows is None else rows
    lines = [",".join(header or HEADER)] + [",".join(r) for r in rows]
    return (sep.join(lines) + sep).encode()


def row(**changes):
    r = dict(zip(HEADER, ROW))
    r.update({k.replace("__", "."): v for k, v in changes.items()})
    return [r[h] for h in HEADER]


def run_csv(content):
    report = validate(content, "route.csv", "fcs-csv")
    return report, [i for s in report.sections for i in s.issues]


def codes(issues, severity=None):
    return sorted(i.code for i in issues if severity is None or i.severity == severity)


def test_valid_csv_has_no_issues():
    _, issues = run_csv(csv_file([ROW, row(Customer__AccountNumber="1002", Meter__MeterNumber="M1002")]))
    assert issues == []


def test_csv_utf16_with_bom_is_read():
    content = csv_file().decode().encode("utf-16")
    _, issues = run_csv(content)
    assert issues == []


def test_first_column_must_be_workset_id():
    header = ["Meter.MeterNumber", "WorkSet.WorkSetID"]
    _, issues = run_csv(csv_file([["M1", "ROUTE001"]], header))
    assert codes(issues) == ["header-first-column"]


def test_required_columns():
    _, issues = run_csv(csv_file([["ROUTE001"]], ["WorkSet.WorkSetID"]))
    assert codes(issues) == ["header-missing"]


def test_unknown_unavailable_and_export_only_columns():
    header = HEADER + ["Meter.MeterNumbr", "Work.SequenceNumber", "MeterSessionInput.ChangeIndicator"]
    _, issues = run_csv(csv_file([ROW + ["", "", "0"]], header))
    assert codes(issues, Severity.ERROR) == ["header-unavailable", "header-unknown"]
    assert codes(issues, Severity.WARNING) == ["header-export-only"]


def test_trailing_comma_and_column_count():
    content = csv_file([ROW]).replace(b"M1001,0,5,0,0,\r\n", b"M1001,0,5,0,0,,\r\n")
    report, issues = run_csv(content)
    [issue] = issues
    assert issue.code == "column-count" and "ends with a comma" in issue.message and issue.line == 2


def test_quoted_comma_is_one_value():
    r = row(Customer__FullName='"SMITH, JOHN"')
    _, issues = run_csv(csv_file([r]))
    assert issues == []


def test_short_route_id_is_padded_but_bad_characters_are_errors():
    _, issues = run_csv(csv_file([row(WorkSet__WorkSetID="11"), row(WorkSet__WorkSetID="R/1", Customer__AccountNumber="2")]))
    assert codes(issues, Severity.ERROR) == ["route-id-characters"]


def test_value_checks_use_the_guide_definitions():
    bad = row(WorkSet__WorkSetType="3", MeterSessionInput__NumberDials="11", Meter__MeterNumber="M" * 15)
    _, issues = run_csv(csv_file([bad]))
    assert codes(issues) == ["invalid-value", "length", "out-of-range"]
    msg = next(i for i in issues if i.code == "out-of-range")
    assert msg.column == 7 and msg.field == "MeterSessionInput.NumberDials" and "from 0 to 10" in msg.message


def test_blank_values_use_defaults_but_meter_number_is_required():
    _, issues = run_csv(csv_file([row(WorkSet__WorkSetType="", MeterSessionInput__NumberDials="", Meter__MeterNumber="")]))
    assert codes(issues) == ["required-blank"]


def test_read_method_needs_matching_input_data():
    rf_missing = row(MeterSessionInput__ReadMethod="3")
    rf_unused = row(Customer__AccountNumber="2", MeterSessionInputRF__EndpointID="12345")
    rf_ok = row(Customer__AccountNumber="3", MeterSessionInput__ReadMethod="3", MeterSessionInputRF__EndpointID="12345")
    _, issues = run_csv(csv_file([rf_missing, rf_unused, rf_ok]))
    assert codes(issues) == ["read-method-missing-data", "read-method-unexpected-data"]


def test_read_method_5_requires_prompt_code_1():
    _, issues = run_csv(csv_file([row(MeterSessionInput__ReadMethod="5", MeterSessionInput__PromptCode="0")]))
    assert codes(issues) == ["read-method-5-prompt"]


def test_workset_values_must_agree_within_route():
    _, issues = run_csv(csv_file([ROW, row(WorkSet__WorkSetType="1", Customer__AccountNumber="2")]))
    assert codes(issues) == ["workset-differs"]


def test_csv_grouped_by_account():
    rows = [ROW, row(MeterSessionInput__NumberDials="99"), row(Customer__AccountNumber="1002", Customer__FullName="DOE JANE")]
    report, _ = run_csv(csv_file(rows))
    assert report.account_count == 2 and report.route_count == 1
    [bad] = [s for s in report.sections if s.issues]
    assert bad.title == "SMITH JOHN — account # 1001, route ROUTE001, starting on line 2"


def test_xml_uploaded_as_csv():
    _, issues = run_csv(b"<?xml version='1.0'?><Import/>")
    assert codes(issues) == ["wrong-format"]


# --- XML ---------------------------------------------------------------------

NS = "http://www.itron.com/nVanta"


def xml_file(meter_session="", customer_extra="", workset_extra="", codes_xml="", ns=NS) -> bytes:
    session = meter_session or """
          <NumberDials>5</NumberDials>
          <NumberDecimals>0</NumberDecimals>
          <PreviousRead>1000</PreviousRead>
          <ConstantMultiplier>1</ConstantMultiplier>
          <ReadMethod>0</ReadMethod>
          <PromptCode>0</PromptCode>
          <TextPrompt>KWH</TextPrompt>
          <TypeOfValidationIndicator>0</TypeOfValidationIndicator>
          <Hi1>500</Hi1><Hi2>600</Hi2><Low1>10</Low1><Low2>5</Low2>"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Import xmlns="{ns}">{codes_xml}
  <WorkSets Ownership="CompanyA">
    <WorkSet>
      <WorkSetID>ROUTE001</WorkSetID>
      <WorkSetType>5</WorkSetType>
      <ScheduledReadDate>2026-10-01</ScheduledReadDate>
      <FilteringInformation>/Root Node</FilteringInformation>
      <Cycle>01</Cycle>
      <CriticalReadDate>2026-10-08</CriticalReadDate>{workset_extra}
      <Work>
        <SequenceNumber>1</SequenceNumber>
        <Customer>
          <FullName>SMITH JOHN</FullName>
          <FullStreetAddress>123 MAIN ST</FullStreetAddress>
          <AccountNumber>1001</AccountNumber>{customer_extra}
          <Meter>
            <UtilityMeterSequenceNumber>10</UtilityMeterSequenceNumber>
            <MeterNumber>M1001</MeterNumber>
            <MeterCategory>0</MeterCategory>
            <Segment>1</Segment>
            <MeterSessionInput>{session}
            </MeterSessionInput>
          </Meter>
        </Customer>
      </Work>
    </WorkSet>
  </WorkSets>
</Import>
""".encode()


def run_xml(content):
    report = validate(content, "import.xml", "fcs-xml")
    return report, [i for s in report.sections for i in s.issues]


def test_valid_xml_has_no_issues():
    report, issues = run_xml(xml_file())
    assert issues == []
    assert report.account_count == 1 and report.route_count == 1


def test_xml_not_well_formed():
    _, issues = run_xml(xml_file().replace(b"</Cycle>", b"</cycle>"))
    [issue] = issues
    assert issue.code == "xml-syntax" and issue.line == 9


def test_xml_wrong_namespace():
    _, issues = run_xml(xml_file(ns="http://example.com/other"))
    assert codes(issues) == ["namespace"]


def test_xml_required_unknown_and_out_of_order():
    content = xml_file().replace(b"<Cycle>01</Cycle>", b"").replace(
        b"<WorkSetType>5</WorkSetType>", b"<Bogus>1</Bogus><WorkSetType>5</WorkSetType>").replace(
        b"<MeterCategory>0</MeterCategory>\n            <Segment>1</Segment>",
        b"<Segment>1</Segment>\n            <MeterCategory>0</MeterCategory>")
    _, issues = run_xml(content)
    assert codes(issues) == ["element-order", "required-missing", "unknown-element"]
    order = next(i for i in issues if i.code == "element-order")
    assert order.path == "WorkSets/WorkSet/Work/Customer/Meter/MeterCategory"


def test_xml_value_checks_and_ampersand():
    content = xml_file().replace(b"<WorkSetType>5", b"<WorkSetType>9").replace(
        b"SMITH JOHN", b"SMITH &amp; SONS").replace(b"2026-10-01", b"10/01/2026")
    _, issues = run_xml(content)
    assert codes(issues) == ["forbidden-character", "invalid-date", "invalid-value"]


def test_xml_one_input_type_and_read_method():
    rf = "<MeterSessionInputRF><RFFrequency>0</RFFrequency><EndpointID>123</EndpointID>" \
         "<WakeupTone>0</WakeupTone><TamperCount1>16</TamperCount1><TamperCount2>16</TamperCount2></MeterSessionInputRF>"
    remote = "<MeterSessionInputRemote><DeviceID>1</DeviceID></MeterSessionInputRemote>"
    content = xml_file().replace(b"<Low2>5</Low2>", f"<Low2>5</Low2>{rf}{remote}".encode())
    _, issues = run_xml(content)
    assert "multiple-inputs" in codes(issues)
    assert codes(issues).count("read-method-unexpected-data") == 2  # ReadMethod is 0


def test_xml_codes_order():
    codes_xml = """
  <Codes>
    <SurveyCodes ForceLoad="true"/>
    <SkipCodes ForceLoad="maybe">
      <SkipCode><Code>IA</Code><Translation>Inaccessible</Translation><ProcessedTypeIndicator>1</ProcessedTypeIndicator></SkipCode>
    </SkipCodes>
  </Codes>"""
    _, issues = run_xml(xml_file(codes_xml=codes_xml))
    assert codes(issues) == ["element-order", "invalid-value"]


def test_xml_export_only_element_is_a_warning():
    content = xml_file().replace(b"<Low2>5</Low2>", b"<Low2>5</Low2><ChangeIndicator>0</ChangeIndicator>")
    _, issues = run_xml(content)
    assert codes(issues, Severity.WARNING) == ["export-only"] and codes(issues, Severity.ERROR) == []


def test_xml_entity_expansion_is_not_resolved():
    bomb = b"""<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;">]>
<Import xmlns="http://www.itron.com/nVanta"><Messages><Message><MessageType>0</MessageType>
<MessageValue>&b;</MessageValue></Message></Messages></Import>"""
    _, issues = run_xml(bomb)  # must not expand; either rejected or checked without expansion
    assert all(len(i.message) < 2000 for i in issues)


@pytest.mark.parametrize("fmt", ["fcs-csv", "fcs-xml"])
def test_empty_upload(fmt):
    report = validate(b"", "x", fmt)
    assert report.errors == 1
