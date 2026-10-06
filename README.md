# Reading System File Validator

A small web app that checks meter-reading host files against their published interface guides. Upload a file and the
app reports every problem it finds: the line, the byte positions, what those bytes are for, and what is wrong. Results
are grouped by account, shown on screen, and can be downloaded as a PDF.

Supported in v1.0:

| Application | Format | Status |
|---|---|---|
| MV-RS | Host Download (fixed-width, 128-byte records) | Available |
| FCS | CSV import | Available |
| FCS | XML import | Available (not yet tested against a real customer file) |
| Temetra | CSV import (New Asset, Data Update, Meter Replacement, Schedule, De-schedule, Historical Reads) | Available |
| Temetra | XML import | Available (not yet tested against a real customer file) |

Validation is strict. A file that passes follows the guide exactly, so it will import into MV-RS, FCS and Temetra.

## Running with Docker

**Before the first start, edit `docker-compose.yml` and replace `YOUR_VPN_IP` with this host's VPN IP address.**
Until you do, `docker compose` stops with `invalid IP address: YOUR_VPN_IP`. Then run:

```sh
docker compose up -d --build
```

The app is then available at `http://<this host's VPN IP>:9898`.

Uploaded files contain customer names and addresses, and the app has no login of its own, so put a reverse proxy in
front of it to handle authentication and TLS. The app listens on port 9898. `docker-compose.yml` has three choices for
publishing it; uncomment exactly one:

- **B (default), proxy on another host over a VPN:** `<this host's VPN IP>:9898:9898`. The app is reachable through
  the VPN only.
- **A, proxy on the same host:** `127.0.0.1:9898:9898`. Only this machine can connect. This is the most locked-down
  choice; use it whenever the proxy runs on this machine.
- **C, no proxy, trusted local network only:** `9898:9898`. Anyone who can reach the host can use it.

Option A cannot be reached from a proxy on a different machine, even over a VPN. Use B for that. The choice is yours.

Settings (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `RSV_MAX_UPLOAD_MB` | 50 | Largest file accepted |
| `RSV_REPORT_TTL_MINUTES` | 30 | How long a finished report stays available for PDF download |
| `RSV_MAX_CACHED_REPORTS` | 20 | How many reports are kept in memory at once |
| `RSV_BASE_PATH` | *(empty)* | Sub-path when the proxy serves the app under one, e.g. `/validator`. Works whether or not the proxy strips the prefix. |

If you see `{"detail":"Not Found"}`, the app is running but received a path it doesn't know. This is usually a proxy
serving it under a sub-path without `RSV_BASE_PATH` set.

Uploaded files are never saved. Uploads larger than 1 MB are briefly buffered in `/tmp` while being read. In the
container, `/tmp` is an in-memory tmpfs (see `docker-compose.yml`) and the rest of the filesystem is read-only, so
nothing reaches disk. Reports are held in memory only until they expire, and a container restart clears them.

## Development

Requires Python 3.12+.

```sh
uv venv && uv pip install -e '.[dev]'
.venv/bin/uvicorn rsvalidator.web:app --reload        # http://127.0.0.1:8000
.venv/bin/python -m pytest                            # all tests
.venv/bin/python -m pytest tests/test_mvrs.py -k rff  # a subset
```

Put real sample files in `samples/`. That directory is git-ignored, and `tests/test_samples.py` runs a smoke test
against any `.dat` files in it. Never commit real files. All committed test fixtures are synthetic
(`tests/mvrs_builder.py`).
