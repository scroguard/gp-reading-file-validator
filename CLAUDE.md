# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
uv venv && uv pip install -e '.[dev]'                  # setup (Python 3.12+)
.venv/bin/python -m pytest                             # all tests
.venv/bin/python -m pytest tests/test_mvrs.py -k rff   # single test / subset
.venv/bin/uvicorn rsvalidator.web:app --reload         # dev server, http://127.0.0.1:8000
docker compose up -d --build                           # container, http://127.0.0.1:9898
```

`tests/test_samples.py` runs against real files in `samples/` when present and is skipped otherwise. Docker is not installed on the dev Mac, so the image build has not been run locally.

## What this is

A Dockerized web app that validates meter-reading-system flat files against the vendors' published interface guides. Users upload a file through a web UI; the app reports every formatting error on screen, with a downloadable PDF. Distributed via git.

v1.0 scope — 5 formats across 3 applications:

| Application | Formats | Notes |
|---|---|---|
| MV-RS | fixed-width flat text | **Build first.** Legacy; historically lax about alpha/numeric field contents |
| FCS | CSV, XML | Strict typing; also imports MV-RS files |
| Temetra | CSV, XML | Strict typing; also imports MV-RS files |

Each application's record headers are proprietary — an FCS CSV is not valid for Temetra and vice versa. The only cross-compatibility is that FCS and Temetra both import MV-RS files, but enforce field typing MV-RS itself never did.

**Validation is always strict.** MV-RS files are validated to the letter of the guide (alpha fields alpha-only, numeric fields numeric-only), so a passing file works in MV-RS, FCS, and Temetra. There is no lenient mode.

The authoritative source for every rule is the published interface guide for each format (supplied by the user). Encode layouts from the guide; do not infer field positions or types from sample files alone.

## Sample files — never commit

User-supplied sample files are real production files containing customer names and addresses. They are for reference only:
- Never commit them, copy them into test fixtures, or quote their personal data in code, docs, commit messages, or tests.
- Keep them only in the git-ignored `samples/` directory.
- Test fixtures must be synthetic, generated from the guide's layouts with fake data.

## Error reporting requirements

Report **all** errors, grouped by account, under a summary table that counts each kind of problem. An account is one CUS and everything under it until the next CUS or route trailer: an optional CSX, then one or more meters (see hierarchy below). Section headings read "NAME — account # X, route Y, starting on line N". Names in real files are "LAST, FIRST", so there is no possessive form. The PDF adds "contains the following errors:".

Each error in the group gives the record type, **line number**, **byte range** (1-based, as in the guides), the field's purpose and allowed content, and what was found:

> In the CUS record on line 541 contains invalid information in bytes 15-34. These bytes are reserved for the account # and can only contain numeric characters, but this record contains alpha-numeric characters.

Errors outside any account, such as FHD, CHD, RHD, route, cycle, or file trailers, and structure or ordering problems, need their own sections. The same report content renders to HTML on screen and to a PDF download.

## MV-RS record structure

Source: `samples/MV-RS_host_interface_guide.pdf` (Itron TDC-0341-024, March 2016), Chapter 1 "Host Download Files", printed pages 7–25 (PDF pages 17–35; printed page = PDF page − 10). The layout diagram on printed page 2 is an image. Data definitions (printed pages 49–103) give allowed values per field. Chapter 2 (Host Upload) is out of scope. Only the fixed-width standard format is in scope, not the comma-separated CSHD variant.

General format rules (printed page 1):
- Every line is exactly 128 bytes: 126 data bytes, then CR/LF in bytes 127–128.
- Types: `A` alpha, `A/N` alphanumeric, `N` numeric, `D` date `MMDDYYYY`, `T` time `HHMMSS`, `H` hex (CR/LF only).
- `A` and `A/N` are left-justified and blank-filled. `N` is right-justified and zero-filled.
- All fields are uppercase except names, addresses, messages, and descriptions.
- Previous Reading, High1, and Low1 carry an implied decimal. The digit count comes from RDG Number of Decimals.

Hierarchy (the file's layout diagram):

```
FHD                              file header (first line)
  [table records]                optional, IDs FC/ML/MT/RI/RT/SC/TC (see "Record ID (Download Table)")
  CHD                            cycle header      ─┐ repeats per cycle
    RHD | ERH                    route header      ─┐ repeats per route (standard OR extended, not both)
      [RTS] [RTM]                survey, message
      CUS [CSX]                  customer          ─┐ repeats per customer = one "account"
        MTR [MTX] [MTS] [PRB]    meter             ─┐ repeats per meter (≥1 per CUS)
          RDG [RFF] [WRR]        reading           ─┐ repeats per reading (≥1 per MTR)
    RTR | ERT                    route trailer (must match the header's standard/extended choice)
  CTR                            cycle trailer (identical to CHD except the record ID)
FTR                              file trailer (identical to FHD except the record ID)
```

A route with more than 9999 readings must use ERH/ERT. Route header and trailer match except for the reading, customer, and meter totals, which must be populated in the trailer.

Flag → record linkages. Check each in both directions:

| Flag | Location | Governs |
|---|---|---|
| Tables Indicator | FHD/FTR byte 4 | table records |
| Optical Probe Indicator | FHD/FTR byte 5 | any PRB in file |
| Off-site Record Indicator | FHD/FTR byte 16 | any RFF in file |
| Wand Record Indicator | FHD/FTR byte 17 | any WRR in file |
| Extended Route Header/Trailer Indicator | FHD/FTR byte 18 | ERH/ERT used |
| Survey Indicator | RHD/ERH byte 12 | RTS after route header |
| Route Message Indicator | RHD/ERH byte 13 | RTM |
| Customer Extra Record Indicator | CUS byte 118 | CSX immediately after CUS |
| MTX Record Indicator | MTR byte 100 | MTX |
| Special Message Indicator | MTR byte 101 | MTS after MTR (and after MTX when present, per the GPS note on printed page 6) |
| Read Method | RDG byte 31 | `P` → PRB, `R` → RFF, `W` → WRR; `K`/`N` → none |

Count and consistency fields also need checks: CUS Number of Meters, MTR Number of Readings, route trailer totals, CHD Number of Routes, FHD Number of Cycles, and the route number repeated in every record, which must match the route header.

The interface guide defines each linkage. Model these rules as data or declarative checks alongside the field layouts, not as scattered ad-hoc conditionals, because FCS and Temetra will add their own sets.

## Architecture

Code is in `src/rsvalidator/`. `engine.validate()` runs four layers. Each layer emits `issues.Issue`, which carries severity, rule code, line, byte range, record, field, message and guide page:

1. `lines.py`: splits lines with 1-based numbering and checks framing: CR/LF endings, record length, and non-printable bytes.
2. `fields.py`: checks each field against the layout in `formats/<format>.yaml` (type/charset, required, enumerated values, range, justification, case, pad). It is generic; nothing in it is specific to one format.
3. `mvrs.TreeBuilder`: places each record into the file → cycle → route → account → meter → reading tree and reports out-of-place or missing records. It records which account or route owns each line; the report groups issues by that.
4. `mvrs.RuleChecker`: cross-record rules. The flag → record linkages are data (`rules:` in `mvrs.yaml`). Counts, header/trailer equality, route and meter number consistency and read sequence are in code.

`engine._build_report` groups issues into sections (file, route, account) and builds the summary. `web.py` renders the report with Jinja templates. It keeps the finished report, never the upload, in an in-memory TTL cache so `pdf.py` (fpdf2) can render the PDF on demand.

To add a format (FCS, Temetra):
- Add a `formats/<key>.yaml`.
- Register it in `engine.FORMATS`.
- Give it its own structure/rules module. The CSV and XML formats will need a different line and field layer.

`mvrs.yaml` byte positions were independently audited against the guide (all correct). The `page` values in it are the guide's printed page numbers.

## Validation policy decisions (from the user)

**What the target systems accept outranks the guide's letter.** `samples/nespelem-download.dat` imported into FCS with no issues. It must produce **zero errors**; `tests/test_samples.py` enforces this when the file is present. Where the guide is stricter than MV-RS and FCS in practice, the user decides case by case, and each decision is recorded as a `note:` in `mvrs.yaml`.

Accepted in practice, even where the guide says otherwise:
- **Numeric fields padded with spaces** on either side (`"  16"`, `"7   "`). Spaces inside the number are still an error.
- **RF Frequency without the decimal point.** The guide shows it as `000952.00625`; real files carry digits only.
- **Names** (CUS Name, CSX Customer Name) may contain digits and punctuation (`text` charset).
- **Account numbers, meter numbers and Optical Probe Recorder IDs** may contain punctuation such as `.` and `-` (`text` charset).
- **Blank Read Type** (RDG).
- **HHF Position `0`** means not used, like a blank.
- **Blank MTS Special Message Text**, as long as the MTR Special Message Indicator is Y.
- **Reserved bytes are not checked** (`reserved: true`).

Still enforced:
- Names, account numbers and meter numbers must be left-justified. Only addresses and free-form message fields may start with spaces (`leading_spaces: true`).
- Other A fields are letters only and other A/N fields are letters and digits.
- A blank required field is an error. A blank optional field (including numeric) is accepted.
- Where the guide contradicts itself, follow its specific notes and examples over its general conventions.
  - Wand Program values include SR00.
  - WRR Device ID may be numeric.

**Severity: warning, not error,** where the guide says MV-RS continues the import or FCS accepted it. That covers:
- Wrong record length.
- Pad bytes that aren't blank.
- Header/trailer mismatches.
- Cycle and route counts in the FHD/CHD.
- Duplicate or zero read sequence numbers (MV-RS renumbers them).
- A file indicator of Y with no matching records.
- Informational route totals (keyed, gas, water, electric, location, extra).

Everything else is an **error**, including the rules MV-RS silently repairs, such as a missing RFF for read method R.

Open questions for the user:
- The `!` in byte 126 of every forest-grove line. It is currently a pad warning.

## Deployment

- Authentication is out of scope for the app. It is expected to sit behind a user-configured reverse proxy, typically reached over a VPN, which handles auth.
- The container listens on **9898**. The Docker configuration should **default to binding to `127.0.0.1`** and strongly recommend that setup. `docker-compose.yml` offers alternatives: bind to the host's VPN IP for a proxy on another host (the user's actual setup), or all interfaces for a trusted LAN. That choice belongs to the user.
- Uploaded files contain customer PII. They are never saved. Uploads over 1 MB are briefly spooled to `/tmp` (python-multipart), which compose mounts as tmpfs, so keep `/tmp` in RAM in any deployment. Reports, which contain names, live only in memory until their TTL expires.
- The interface guide PDF is marked proprietary and confidential by Itron. It stays in git-ignored `samples/` and is never committed. The repo, which contains field layouts derived from it, was made public on 2026-09-28; the user says they have permission.
