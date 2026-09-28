"""Smoke test against real sample files, if present locally.

samples/ is git-ignored (real customer data), so this is skipped in CI and on
fresh clones. It asserts only that validation completes, and prints nothing
from the files.
"""

from pathlib import Path

import pytest

from rsvalidator.engine import validate
from rsvalidator.pdf import render_pdf

SAMPLES = sorted((Path(__file__).parent.parent / "samples").glob("*.dat"))


@pytest.mark.skipif(not SAMPLES, reason="no local sample files")
@pytest.mark.parametrize("path", SAMPLES, ids=[p.name for p in SAMPLES])
def test_sample_file_validates(path):
    report = validate(path.read_bytes(), path.name)
    assert report.line_count > 0 and report.account_count > 0


@pytest.mark.skipif(not SAMPLES, reason="no local sample files")
def test_sample_pdf_renders():
    report = validate(SAMPLES[0].read_bytes(), SAMPLES[0].name)
    assert render_pdf(report).startswith(b"%PDF")


FCS_ACCEPTED = Path(__file__).parent.parent / "samples" / "nespelem-download.dat"


@pytest.mark.skipif(not FCS_ACCEPTED.exists(), reason="sample not present")
def test_file_accepted_by_fcs_has_no_errors():
    # FCS imported this file without issues; everything in it must pass (warnings allowed).
    report = validate(FCS_ACCEPTED.read_bytes(), FCS_ACCEPTED.name)
    assert report.errors == 0
