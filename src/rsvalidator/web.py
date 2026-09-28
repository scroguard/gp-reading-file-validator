"""FastAPI app: upload a file, see the report, download it as PDF.

Uploaded files are never written to disk. The finished report (not the file)
is kept in memory for a short time so the PDF can be downloaded.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .engine import FORMATS, Report, validate
from .pdf import render_pdf

MAX_UPLOAD_MB = int(os.environ.get("RSV_MAX_UPLOAD_MB", "50"))
REPORT_TTL_SECONDS = int(os.environ.get("RSV_REPORT_TTL_MINUTES", "30")) * 60
MAX_CACHED_REPORTS = int(os.environ.get("RSV_MAX_CACHED_REPORTS", "20"))
# Sub-path the app is served under by a reverse proxy, e.g. "/validator". Empty = site root.
BASE_PATH = os.environ.get("RSV_BASE_PATH", "").strip().strip("/")
BASE_PATH = f"/{BASE_PATH}" if BASE_PATH else ""

HERE = Path(__file__).parent
app = FastAPI(title="Reading System File Validator", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["version"] = __version__


class BasePathMiddleware:
    """Serves the app under a sub-path, whether or not the proxy strips it.

    Requests for /validator/x and /x both reach route /x, and generated links
    carry the /validator prefix.
    """

    def __init__(self, app, base: str):
        self.app = app
        self.base = base

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            if path == self.base:
                path = self.base + "/"
            elif not path.startswith(self.base + "/"):
                path = self.base + path
            scope = dict(scope, path=path, root_path=self.base)
        await self.app(scope, receive, send)


if BASE_PATH:
    app.add_middleware(BasePathMiddleware, base=BASE_PATH)


class ReportCache:
    def __init__(self):
        self._items: OrderedDict[str, tuple[float, Report]] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, report: Report) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._expire()
            self._items[token] = (time.monotonic(), report)
            while len(self._items) > MAX_CACHED_REPORTS:
                self._items.popitem(last=False)
        return token

    def get(self, token: str) -> Report | None:
        with self._lock:
            self._expire()
            item = self._items.get(token)
            return item[1] if item else None

    def _expire(self):
        cutoff = time.monotonic() - REPORT_TTL_SECONDS
        for token in [t for t, (ts, _) in self._items.items() if ts < cutoff]:
            del self._items[token]


reports = ReportCache()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"formats": FORMATS, "max_mb": MAX_UPLOAD_MB})


@app.post("/validate", response_class=HTMLResponse)
async def validate_upload(request: Request, file: UploadFile = File(...), format: str = Form("mvrs")):
    if format not in FORMATS:
        raise HTTPException(400, "Unknown file format.")
    content = await file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
        return templates.TemplateResponse(
            request, "index.html",
            {"formats": FORMATS, "max_mb": MAX_UPLOAD_MB, "error": f"The file is larger than {MAX_UPLOAD_MB} MB."},
            status_code=413,
        )
    if not content:
        return templates.TemplateResponse(
            request, "index.html",
            {"formats": FORMATS, "max_mb": MAX_UPLOAD_MB, "error": "The uploaded file is empty."},
            status_code=400,
        )
    report = validate(content, file.filename or "upload", format)
    del content
    token = reports.put(report)
    return templates.TemplateResponse(request, "report.html", {"report": report, "token": token})


@app.get("/report/{token}.pdf")
def report_pdf(token: str):
    report = reports.get(token)
    if report is None:
        raise HTTPException(404, "This report has expired. Upload the file again to regenerate it.")
    stem = Path(report.filename).stem or "report"
    return Response(
        render_pdf(report),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{stem}-validation.pdf"'},
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
