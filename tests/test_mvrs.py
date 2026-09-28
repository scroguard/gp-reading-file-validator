from mvrs_builder import ROUTE, account, build_file, record

from rsvalidator.engine import validate
from rsvalidator.issues import Severity


def run(content: bytes):
    report = validate(content, "test.dat")
    issues = [i for s in report.sections for i in s.issues]
    return report, issues


def codes(issues, severity=None):
    return sorted(i.code for i in issues if severity is None or i.severity == severity)


def only(issues, code):
    found = [i for i in issues if i.code == code]
    assert found, f"no {code!r} issue in {codes(issues)}"
    return found


# --- baseline -----------------------------------------------------------------


def test_valid_file_has_no_issues():
    _, issues = run(build_file(account(0) + account(1)))
    assert issues == []


def test_valid_file_with_all_optional_records():
    body = account(0, cus=dict(customer_extra_indicator="Y"),
                   mtr=dict(mtx_indicator="Y", special_message_indicator="Y", special_message_display="1"),
                   extra={"CSX": {}, "MTX": {}, "MTS": {}})
    body += account(1, read_method="W", extra={"WRR": {}})
    body += account(2, read_method="P", mtr=dict(optical_probe_recorder_id="M1001"), extra={"PRB": {}})
    body += account(3, read_method="K")
    _, issues = run(build_file(body))
    assert issues == []


def test_extended_route_records_are_valid():
    _, issues = run(build_file(account(0), extended=True))
    assert issues == []


# --- field checks ------------------------------------------------------------


def test_message_names_record_line_bytes_and_field():
    # The report format the user asked for: record, line, byte range, field purpose, what was wrong.
    _, issues = run(build_file(account(0, mtr=dict(meter_type="A1"))))
    [issue] = only(issues, "invalid-characters")
    assert issue.line == 5 and issue.record == "MTR" and (issue.start, issue.end) == (60, 61)
    assert issue.message.startswith(
        "The MTR record on line 5 contains invalid information in bytes 60-61. "
        "These bytes are reserved for the Meter Type. These bytes can only contain numeric characters")


def test_names_accept_digits_and_punctuation():
    # User decision: FCS accepts these even though Name is typed A.
    _, issues = run(build_file(account(0, cus=dict(name="O'BRIEN (RENTAL) #2"))))
    assert issues == []


def test_alpha_field_rejects_digits():
    _, issues = run(build_file(account(0, mtr=dict(bill_code="1"))))
    [issue] = only(issues, "invalid-characters")
    assert issue.field == "Bill Code" and "'1'" in issue.message


def test_account_number_accepts_punctuation():
    _, issues = run(build_file(account(0, cus=dict(account_number="123-4567.8"))))
    assert issues == []


def test_meter_number_accepts_punctuation():
    body = account(0, mtr=dict(meter_number="12-345.6", optical_probe_recorder_id="12-345.6"),
                   read_method="P", extra={"PRB": {"optical_probe_recorder_id": "12-345.6"}})
    _, issues = run(build_file(body))
    assert issues == []


def test_alphanumeric_field_rejects_punctuation():
    _, issues = run(build_file(account(0, rdg=dict(text_prompt="KW.H"))))
    [issue] = only(issues, "invalid-characters")
    assert (issue.start, issue.end) == (12, 15) and "'.'" in issue.message


def test_lowercase_only_allowed_in_names_addresses_and_messages():
    _, issues = run(build_file(account(0, mtr=dict(meter_number="m1001"),
                                       cus=dict(name="Smith, John", address_1="12 Elm st"))))
    [issue] = only(issues, "lowercase")
    assert issue.field == "Meter Number"


def test_left_justification():
    body = account(0, cus=dict(account_number="       1234567", address_1="      MAIN ST"))
    _, issues = run(build_file(body))
    [issue] = only(issues, "not-left-justified")
    assert issue.field == "Account Number"


def test_numeric_fields_may_be_space_padded():
    # Seen in every sample and accepted by MV-RS and FCS: "  16" instead of "0016", "7   " for a tone.
    body = account(0, extra={"RFF": {"tamper": "  16", "rf_tone": "4   "}})
    _, issues = run(build_file(body))
    assert issues == []


def test_numeric_field_with_inner_space_is_an_error():
    _, issues = run(build_file(account(0, extra={"RFF": {"tamper": "1 6 "}})))
    [issue] = only(issues, "invalid-characters")
    assert issue.field == "Tamper" and "space" in issue.message


def test_blank_optional_numeric_field_is_accepted():
    _, issues = run(build_file(account(0, mtr=dict(meter_type="  ", time_code="   "))))
    assert issues == []


def test_blank_required_field():
    _, issues = run(build_file(account(0, rdg=dict(prompt_code=" "))))
    [issue] = only(issues, "required-blank")
    assert issue.field == "Prompt Code" and issue.record == "RDG"


def test_blank_read_type_is_accepted():
    _, issues = run(build_file(account(0, rdg=dict(read_type="  "))))
    assert issues == []


def test_trailer_only_required_field_is_optional_in_header():
    _, issues = run(build_file(route=dict(total_customer_records="    ")))
    blanks = only(issues, "required-blank")
    assert [i.record for i in blanks] == ["RTR"]


def test_hhf_position_zero_means_not_used():
    _, issues = run(build_file(account(0, rdg=dict(hhf_position="0")) + account(1, rdg=dict(hhf_position="4"))))
    [issue] = only(issues, "invalid-value")
    assert issue.field == "HHF Position" and issue.line == 10


def test_invalid_enumerated_value():
    _, issues = run(build_file(account(0, mtr=dict(meter_category="X"))))
    [issue] = only(issues, "invalid-value")
    assert issue.field == "Meter Category" and "E, G, I, S, W" in issue.message


def test_invalid_date():
    _, issues = run(build_file().replace(b"07132026", b"13322026"))
    assert {i.record for i in only(issues, "invalid-date")} == {"CHD", "CTR"}


def test_rf_frequency_decimal_point_is_optional():
    body = account(0, extra={"RFF": {"rf_frequency": "000000000000"}})
    body += account(1, extra={"RFF": {"rf_frequency": "", "geographic_area": "12"}})
    body += account(2, extra={"RFF": {"rf_frequency": "00"}})
    body += account(3, extra={"RFF": {"rf_frequency": "952.006.25"}})
    _, issues = run(build_file(body))
    [issue] = only(issues, "rf-frequency-format")
    assert issue.line == 19


def test_reserved_bytes_are_not_checked():
    body = account(0, extra={"RFF": {"reserved_2": "ERT,"}})
    _, issues = run(build_file(body))
    assert issues == []


def test_route_number_rules():
    bad = "01 00001"
    body = [ln[:3] + bad + ln[11:] if ln[:3] == "CUS" else ln for ln in account(0)]
    _, issues = run(build_file(body))
    assert only(issues, "route-number-format")[0].line == 4
    assert only(issues, "route-number-mismatch")[0].line == 4


def test_pad_must_be_blank():
    body = [ln[:125] + "X" if ln[:3] == "CUS" else ln for ln in account(0)]
    _, issues = run(build_file(body))
    assert only(issues, "pad-not-blank")[0].severity == Severity.WARNING


# --- framing -----------------------------------------------------------------


def test_line_too_long_is_reported():
    content = build_file().replace(record("RDG").encode(), record("RDG").encode() + b" ", 1)
    _, issues = run(content)
    [issue] = only(issues, "line-length")
    assert issue.line == 6 and "1 byte(s) too long" in issue.message
    assert issue.severity == Severity.WARNING


def test_lf_line_endings_reported_once():
    _, issues = run(build_file().replace(b"\r\n", b"\n"))
    assert codes(issues) == ["line-ending"]


def test_unknown_record_type():
    content = build_file().replace(record("RFF").encode(), b"XYZ" + record("RFF").encode()[3:])
    _, issues = run(content)
    assert only(issues, "unknown-record")[0].line == 7


def test_non_ascii_character():
    content = build_file().replace(b"SMITH, JOHN", "MUÑOZ, ANA".encode("latin-1"))
    _, issues = run(content)
    assert only(issues, "non-printable-character")[0].line == 4


# --- structure ---------------------------------------------------------------


def test_missing_file_trailer():
    content = build_file().rsplit(b"FTR", 1)[0]
    _, issues = run(content)
    assert "missing-record" in codes(issues)


def test_csx_must_follow_customer():
    body = account(0)
    body.insert(3, record("CSX"))  # after MTR/RDG instead of CUS
    _, issues = run(build_file(body))
    only(issues, "record-out-of-place")


def test_customer_without_meter():
    body = [record("CUS", account_number="1", number_of_meters="0")] + account(1)
    _, issues = run(build_file(body))
    assert any(i.code == "missing-record" and i.line == 4 for i in issues)


def test_standard_header_with_extended_trailer():
    content = build_file().replace(b"\r\nRTR", b"\r\nERT", 1)
    _, issues = run(content)
    assert "record-out-of-place" in codes(issues)


# --- cross-record rules ------------------------------------------------------


def test_read_method_radio_requires_rff():
    _, issues = run(build_file(account(0, extra={"RFF": None})))
    [issue] = only(issues, "read-method-missing-record")
    assert issue.record == "RDG" and (issue.start, issue.end) == (31, 31)


def test_rff_with_keyed_read_method_is_an_error():
    # The user's example: an RFF record present while the read method is K (manual).
    _, issues = run(build_file(account(0, read_method="K", extra={"RFF": {}})))
    [issue] = only(issues, "read-method-unexpected-record")
    assert issue.record == "RFF" and '"K" (Keyed)' in issue.message


def test_wand_and_probe_linkage():
    body = account(0, read_method="W", extra={"WRR": None})  # W without WRR
    body += account(1, read_method="K", mtr=dict(optical_probe_recorder_id="M1001"), extra={"PRB": {}})  # PRB without P
    _, issues = run(build_file(body))
    assert codes(issues, Severity.ERROR) == ["read-method-missing-record", "read-method-unexpected-record"]


def test_customer_extra_indicator_both_directions():
    body = account(0, cus=dict(customer_extra_indicator="Y"))  # Y, no CSX
    body += account(1, extra={"CSX": {}})  # CSX, indicator N
    _, issues = run(build_file(body))
    assert codes(issues) == ["indicator-missing-record", "indicator-unexpected-record"]


def test_special_message_indicator_requires_display_code():
    body = account(0, mtr=dict(special_message_indicator="Y", special_message_display="0"), extra={"MTS": {}})
    _, issues = run(build_file(body))
    assert codes(issues) == ["special-message-display"]


def test_blank_mts_text_is_accepted_when_flagged():
    body = account(0, mtr=dict(special_message_indicator="Y", special_message_display="3"),
                   extra={"MTS": {"special_message_text": ""}})
    _, issues = run(build_file(body))
    assert issues == []


def test_blank_mts_without_indicator_is_still_an_error():
    _, issues = run(build_file(account(0, extra={"MTS": {"special_message_text": ""}})))
    assert codes(issues) == ["indicator-unexpected-record"]


def test_mts_meter_number_must_match_meter():
    body = account(0, extra={"MTS": {"meter_number": "OTHER"}})
    body[1] = record("MTR", meter_read_sequence="10", special_message_indicator="Y", special_message_display="1")
    _, issues = run(build_file(body))
    assert codes(issues) == ["meter-number-mismatch"]


def test_meter_and_reading_counts():
    body = account(0, cus=dict(number_of_meters="2"), mtr=dict(number_of_readings="3"))
    _, issues = run(build_file(body))
    assert [i.field for i in only(issues, "count-mismatch")] == ["Number of Meters", "Number of Readings"]


def test_route_trailer_totals():
    _, issues = run(build_file(account(0) + account(1), route=dict(total_reading_records="5")))
    [issue] = only(issues, "count-mismatch")
    assert issue.record == "RTR" and "It says 5, but the route contains 2 Reading (RDG) record(s)" in issue.message


def test_display_totals_are_warnings():
    _, issues = run(build_file(route=dict(total_gas_meters="3")))
    assert codes(issues, Severity.WARNING) == ["total-mismatch"]
    assert codes(issues, Severity.ERROR) == []


def test_header_trailer_must_match():
    content = build_file().replace(b"\r\nCTR01", b"\r\nCTR02", 1)
    _, issues = run(content)
    assert "header-trailer-mismatch" in codes(issues)


def test_route_number_must_start_with_cycle():
    content = build_file().replace(b"CHD01", b"CHD02").replace(b"CTR01", b"CTR02")
    _, issues = run(content)
    assert codes(issues) == ["route-cycle-mismatch"]


def test_file_indicators():
    # WRR present but indicator N -> error. PRB indicator Y with no PRB -> warning.
    body = account(0, read_method="W", extra={"WRR": {}})
    _, issues = run(build_file(body, header=dict(wand_indicator="N", optical_probe_indicator="Y")))
    assert codes(issues, Severity.ERROR) == ["file-indicator"]
    assert codes(issues, Severity.WARNING) == ["file-indicator-unused"]


def test_read_sequence_must_increase_and_be_unique():
    body = account(0, mtr=dict(meter_read_sequence="30")) + account(1, mtr=dict(meter_read_sequence="20"))
    body += account(2, mtr=dict(meter_read_sequence="30"))
    _, issues = run(build_file(body))
    assert codes(issues, Severity.WARNING) == ["read-sequence-decreases", "read-sequence-duplicate"]
    assert codes(issues, Severity.ERROR) == []


def test_read_method_n_requires_prompt_code_p_or_n():
    _, issues = run(build_file(account(0, read_method="N", rdg=dict(prompt_code="Y"))))
    assert codes(issues) == ["read-method-n"]


def test_more_than_9999_readings_needs_extended_route():
    body = [record("CUS", number_of_meters="1"), record("MTR", number_of_readings="999")]
    body += [record("RDG", read_method="K")] * 10000
    content = build_file(body, route=dict(total_reading_records="0000", total_keyed_readings="0000"))
    _, issues = run(content)
    assert "extended-route-required" in codes(issues)


# --- report grouping ---------------------------------------------------------


def test_issues_are_grouped_by_account():
    body = account(0) + account(1, mtr=dict(meter_category="X"), cus=dict(name="DOE, JANE"))
    report, _ = run(build_file(body))
    bad = [s for s in report.sections if s.kind == "account" and s.issues]
    assert len(bad) == 1
    assert bad[0].title == f"DOE, JANE — account # 100201, route {ROUTE}, starting on line 8"
    assert report.accounts_with_errors == 1
    [row] = report.summary
    assert row.count == 1 and row.accounts == 1 and row.first_line == 9


# --- user decisions on real-file patterns ---------------------------------------


def test_433_protocol_and_iwn_encoder_are_flagged_for_reference():
    body = account(0, extra={"RFF": {"protocol_433": "0", "iwn_encoder": "A"}})
    _, issues = run(build_file(body))
    assert codes(issues, Severity.WARNING) == ["rarely-used", "rarely-used"]
    assert codes(issues, Severity.ERROR) == []
    assert "not one of the guide's values" in only(issues, "rarely-used")[0].message


def test_blank_concentrator_ert_is_a_warning():
    _, issues = run(build_file(account(0, extra={"RFF": {"concentrator_ert": " "}})))
    [issue] = issues
    assert issue.code == "required-blank" and issue.severity == Severity.WARNING


def test_account_number_starting_with_spaces_is_a_warning():
    _, issues = run(build_file(account(0, cus=dict(account_number="       1234567"))))
    [issue] = issues
    assert issue.code == "not-left-justified" and issue.severity == Severity.WARNING


def test_all_zero_segment_code_is_highly_recommended():
    # FCS accepts it; it may cause problems in Temetra.
    _, issues = run(build_file(account(0, cus=dict(segment_code="0"))))
    [issue] = issues
    assert issue.code == "segment-all-zeros" and issue.severity == Severity.RECOMMENDED
    assert "Temetra" in issue.message
