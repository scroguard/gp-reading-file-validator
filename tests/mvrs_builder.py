"""Builds synthetic MV-RS files from the layout spec.

Test fixtures are always synthetic: real sample files contain customer data
and must never be committed (see CLAUDE.md).
"""

from __future__ import annotations

from rsvalidator.spec import load_format

SPEC = load_format("mvrs")
ROUTE = "01000001"

# Values that make each record valid. Totals are filled in by build_file().
DEFAULTS: dict[str, dict[str, str]] = {
    "FHD": dict(tables_indicator="N", optical_probe_indicator="N", number_of_cycles="1",
                offsite_indicator="Y", wand_indicator="N", extended_route_indicator="N"),
    "CHD": dict(cycle_id="1", number_of_routes="1", cycle_date="07132026"),
    "RHD": dict(route_number=ROUTE, survey_indicator="N", route_message_indicator="N"),
    "CUS": dict(route_number=ROUTE, number_of_meters="1", account_number="100200", name="SMITH, JOHN",
                address_1="123 Main St", group_code="0", customer_extra_indicator="N"),
    "CSX": dict(route_number=ROUTE, account_number="100200", customer_name="JUNIOR", address_3="Apt 4"),
    "MTR": dict(route_number=ROUTE, number_of_readings="1", group_code="0", meter_status="A",
                meter_number="M1001", meter_type="0", meter_read_sequence="10", special_message_display="0",
                mtx_indicator="N", special_message_indicator="N", meter_category="E", time_code="0"),
    "MTX": dict(route_number=ROUTE, meter_number="M1001", latitude="45.9539084", longitude="-109.83039217"),
    "MTS": dict(route_number=ROUTE, meter_number="M1001", special_message_text="Dog in yard"),
    "RDG": dict(route_number=ROUTE, text_prompt="KWH", prompt_code="Y", read_direction="L", compare_code="0",
                validation_code="0", channel_number="0", number_of_dials="5", number_of_decimals="0",
                read_method="R", previous_reading="1000", high1="500", low1="10", meter_constant="1",
                constant_flag="0", read_type="0"),
    "RFF": dict(route_number=ROUTE, rf_ert_id="1234567890", geographic_area="0", rf_frequency="000952.00625",
                rf_tone="4", tamper="16", concentrator_ert="N"),
    "WRR": dict(route_number=ROUTE, device_id="12345678901234", wand_program="VERS", node_number="0"),
    "PRB": dict(route_number=ROUTE, optical_probe_recorder_id="M1001", device_id="DEV1", tim_name="T1",
                id_mismatch="N", visual_read="Y", tou_register_reads="N", reset_demand="N", dst_update="N",
                status_check="N", encoder_indicator="N"),
}
DEFAULTS["FTR"] = DEFAULTS["FHD"]
DEFAULTS["CTR"] = DEFAULTS["CHD"]
DEFAULTS["RTR"] = DEFAULTS["RHD"]
DEFAULTS["ERH"] = DEFAULTS["ERT"] = DEFAULTS["RHD"]


def record(rid: str, **values: str) -> str:
    """One 126-byte record line (no CR/LF). Values are justified per field type."""
    rec = SPEC.record(rid)
    line = [" "] * SPEC.data_length
    line[0 : len(rid)] = rid
    merged = {**DEFAULTS.get(rid, {}), **values}
    for name, value in merged.items():
        f = rec.field(name)
        if f.type == "N" and value.isdigit():
            text = value.rjust(f.length, "0")
        else:
            text = value.ljust(f.length)
        assert len(text) == f.length, (rid, name, value)
        line[f.start - 1 : f.end] = text
    return "".join(line)


def account(n: int = 0, *, read_method: str = "R", cus: dict | None = None, mtr: dict | None = None,
            rdg: dict | None = None, extra: dict[str, dict] | None = None) -> list[str]:
    """Records for one customer with one meter and one reading.

    `extra` adds optional child records, e.g. {"CSX": {}, "MTS": {}}.
    """
    extra = extra or {}
    seq = str(10 * (n + 1))
    lines = [record("CUS", **{"account_number": str(100200 + n), **(cus or {})})]
    if "CSX" in extra:
        lines.append(record("CSX", **extra["CSX"]))
    lines.append(record("MTR", **{"meter_read_sequence": seq, **(mtr or {})}))
    for rid in ("MTX", "MTS", "PRB"):
        if rid in extra:
            lines.append(record(rid, **extra[rid]))
    lines.append(record("RDG", **{"read_method": read_method, **(rdg or {})}))
    if read_method == "R" and "RFF" not in extra:
        lines.append(record("RFF"))
    for rid in ("RFF", "WRR"):
        if rid in extra and extra[rid] is not None:
            lines.append(record(rid, **extra[rid]))
    return lines


def _route_totals(body: list[str]) -> dict[str, str]:
    ids = [ln[:3] for ln in body]
    rdgs = [ln for ln in body if ln[:3] == "RDG"]
    mtrs = [ln for ln in body if ln[:3] == "MTR"]
    return dict(
        total_reading_records=str(len(rdgs)),
        total_customer_records=str(ids.count("CUS")),
        total_meter_records=str(len(mtrs)),
        total_keyed_readings=str(sum(r[30] == "K" for r in rdgs)),
        total_optical_probe_readings=str(sum(r[30] == "P" for r in rdgs)),
        total_offsite_readings=str(sum(r[30] == "R" for r in rdgs)),
        total_wand_reads=str(sum(r[30] == "W" for r in rdgs)),
        total_electric_meters=str(sum(m[101] == "E" for m in mtrs)),
        total_gas_meters=str(sum(m[101] == "G" for m in mtrs)),
        total_water_meters=str(sum(m[101] == "W" for m in mtrs)),
    )


def build_file(body: list[str] | None = None, *, header: dict | None = None, route: dict | None = None,
               extended: bool = False) -> bytes:
    """A complete, valid file: FHD, CHD, one route containing `body`, trailers."""
    body = body if body is not None else account()
    ids = {ln[:3] for ln in body}
    fhd = {
        "optical_probe_indicator": "Y" if "PRB" in ids else "N",
        "offsite_indicator": "Y" if "RFF" in ids else "N",
        "wand_indicator": "Y" if "WRR" in ids else "N",
        "extended_route_indicator": "Y" if extended else "N",
        **(header or {}),
    }
    totals = _route_totals(body)
    route_values = {**totals, **(route or {})}
    head_id, tail_id = ("ERH", "ERT") if extended else ("RHD", "RTR")
    lines = [
        record("FHD", **fhd),
        record("CHD"),
        record(head_id, **route_values),
        *body,
        record(tail_id, **route_values),
        record("CTR"),
        record("FTR", **fhd),
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")
