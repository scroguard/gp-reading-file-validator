import re

import pytest
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
    assert "Nothing must be corrected" in r.text


def test_report_lists_errors_and_pdf_downloads():
    r = upload(build_file(account(0, mtr=dict(meter_category="X"))))
    assert "1 problem must be corrected" in r.text and "Meter Category" in r.text
    [pdf_url] = re.findall(r'action="([^"]*/report/[^"]+\.pdf)"', r.text)
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


def test_links_are_paths_not_absolute_urls():
    # Behind a reverse proxy the Host header may be the container's, so links must not include it.
    r = client.get("/", headers={"host": "127.0.0.1:9898"})
    assert 'href="/static/app.css"' in r.text and "127.0.0.1" not in r.text


@pytest.mark.parametrize("strip_prefix", [True, False], ids=["proxy-strips-prefix", "proxy-keeps-prefix"])
def test_served_under_a_sub_path(strip_prefix):
    sub = TestClient(web.BasePathMiddleware(web.app, "/validator"))
    prefix = "" if strip_prefix else "/validator"
    page = sub.get(prefix + "/")
    assert page.status_code == 200
    assert 'href="/validator/static/app.css"' in page.text and 'action="/validator/validate"' in page.text
    assert sub.get(prefix + "/static/app.css").status_code == 200
    r = sub.post(prefix + "/validate", data={"format": "mvrs"},
                 files={"file": ("a.dat", build_file(), "text/plain")})
    [pdf_url] = re.findall(r'action="([^"]*/report/[^"]+\.pdf)"', r.text)
    assert pdf_url.startswith("/validator/report/")
    path = pdf_url if not strip_prefix else pdf_url.removeprefix("/validator")
    assert sub.get(path).status_code == 200


def test_bare_sub_path_serves_the_upload_page():
    sub = TestClient(web.BasePathMiddleware(web.app, "/validator"))
    assert sub.get("/validator").status_code == 200


def test_pdf_includes_customer_name():
    r = upload(build_file(), name="route7.dat")
    [pdf_url] = re.findall(r'action="([^"]*/report/[^"]+\.pdf)"', r.text)
    pdf = client.get(pdf_url, params={"customer": "  City of  Springfield "})
    assert pdf.status_code == 200
    assert 'filename="city-of-springfield-route7-validation.pdf"' in pdf.headers["content-disposition"]
    assert b"generated for City of Springfield" in _pdf_text(pdf.content)


def _pdf_text(content: bytes) -> bytes:
    import zlib
    out = b""
    for chunk in content.split(b"stream")[1:]:
        data = chunk.split(b"endstream")[0].strip(b"\r\n")
        try:
            out += zlib.decompress(data)
        except zlib.error:
            pass
    return out


def test_highly_recommended_level_is_shown():
    r = upload(build_file(account(0, cus=dict(segment_code="0000"))))
    assert "Nothing must be corrected, but 1 correction is highly recommended" in r.text
    assert '<span class="tag recommended">Highly recommended</span>' in r.text
