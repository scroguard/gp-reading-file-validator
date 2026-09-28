import re

from fastapi.testclient import TestClient
from mvrs_builder import account, build_file

from rsvalidator import web

client = TestClient(web.app)


def upload(content: bytes, name: str = "route.dat"):
    return client.post("/validate", data={"format": "mvrs"}, files={"file": (name, content, "text/plain")})


def test_upload_page():
    r = client.get("/")
    assert r.status_code == 200 and "MV-RS Host Download" in r.text


def test_valid_file_report():
    r = upload(build_file())
    assert r.status_code == 200
    assert "No errors found" in r.text


def test_report_lists_errors_and_pdf_downloads():
    r = upload(build_file(account(0, mtr=dict(meter_category="X"))))
    assert "1 error found" in r.text and "Meter Category" in r.text
    [pdf_url] = re.findall(r'href="([^"]*/report/[^"]+\.pdf)"', r.text)
    pdf = client.get(pdf_url)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF") and 'filename="route-validation.pdf"' in pdf.headers["content-disposition"]


def test_expired_report_link():
    assert client.get("/report/not-a-token.pdf").status_code == 404


def test_empty_and_oversize_uploads(monkeypatch):
    assert upload(b"").status_code == 400
    monkeypatch.setattr(web, "MAX_UPLOAD_MB", 0)
    assert upload(b"FHD").status_code == 413


def test_unknown_format_rejected():
    r = client.post("/validate", data={"format": "nope"}, files={"file": ("a.dat", b"x", "text/plain")})
    assert r.status_code == 400
