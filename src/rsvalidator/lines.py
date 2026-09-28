"""Splits an uploaded fixed-width file into numbered lines and checks framing."""

from __future__ import annotations

from dataclasses import dataclass

from .issues import Issue, Severity


@dataclass
class Line:
    number: int  # 1-based
    data: str  # line content without the line terminator
    ending: str  # "\r\n", "\n", "\r" or "" (last line with no terminator)


def split_lines(content: bytes) -> tuple[list[Line], list[Issue]]:
    issues: list[Issue] = []
    bad_bytes = [i for i, b in enumerate(content) if b > 0x7E or (b < 0x20 and b not in (0x0A, 0x0D))]
    text = content.decode("latin-1")

    lines: list[Line] = []
    pos, n = 0, 0
    while pos < len(text):
        nl = text.find("\n", pos)
        if nl == -1:
            data, ending, pos = text[pos:], "", len(text)
        else:
            data, ending, pos = text[pos:nl], "\n", nl + 1
            if data.endswith("\r"):
                data, ending = data[:-1], "\r\n"
        n += 1
        lines.append(Line(n, data, ending))

    # A trailing DOS end-of-file marker (Ctrl-Z) or blank lines at the very end
    # are not records.
    while lines and lines[-1].data.strip("\x1a \t") == "":
        lines.pop()

    if bad_bytes:
        # Report per line so the user can find them.
        offsets = set(bad_bytes)
        start = 0
        for line in lines:
            length = len(line.data)
            for i in range(length):
                if start + i in offsets:
                    ch = line.data[i]
                    issues.append(Issue(
                        severity=Severity.ERROR,
                        code="non-printable-character",
                        summary="Non-printable or non-ASCII character",
                        line=line.number,
                        start=i + 1,
                        message=(
                            f"Line {line.number} contains a non-printable or non-ASCII character "
                            f"(hex {ord(ch):02X}) at byte {i + 1}. Records may only contain printable ASCII text."
                        ),
                    ))
            start += length + len(line.ending)
    return lines, issues


def check_framing(lines: list[Line], data_length: int, line_length: int) -> list[Issue]:
    issues: list[Issue] = []
    bad_endings = [ln for ln in lines if ln.ending != "\r\n"]
    # A missing terminator on the final line is its own, single problem.
    last_unterminated = lines and lines[-1].ending == ""
    lf_only = [ln for ln in bad_endings if ln.ending != ""]
    if lf_only:
        issues.append(Issue(
            severity=Severity.ERROR,
            code="line-ending",
            summary="Lines not terminated with CR/LF",
            line=lf_only[0].number,
            start=line_length - 1,
            end=line_length,
            message=(
                f"{len(lf_only)} line(s), starting with line {lf_only[0].number}, are not terminated with a "
                f"carriage return/line feed (CR/LF). Every record must end with CR/LF in bytes "
                f"{line_length - 1}-{line_length}."
            ),
        ))
    if last_unterminated:
        issues.append(Issue(
            severity=Severity.ERROR,
            code="line-ending",
            summary="Lines not terminated with CR/LF",
            line=lines[-1].number,
            start=line_length - 1,
            end=line_length,
            message=(
                f"The last line (line {lines[-1].number}) is not terminated with a carriage return/line feed "
                f"(CR/LF). Every record, including the last, must end with CR/LF."
            ),
        ))
    for ln in lines:
        if len(ln.data) != data_length:
            diff = len(ln.data) - data_length
            what = f"{abs(diff)} byte(s) too {'long' if diff > 0 else 'short'}"
            # FCS accepts records of the wrong length (it accepted a file with 127-byte
            # records), so this is a warning. Shifted fields still produce their own findings.
            issues.append(Issue(
                severity=Severity.WARNING,
                code="line-length",
                summary=f"Record length is not {line_length} bytes",
                line=ln.number,
                record=ln.data[:3] or None,
                message=(
                    f"The {ln.data[:3] or 'blank'} record on line {ln.number} is {len(ln.data) + 2} bytes long "
                    f"including CR/LF ({what}). The guide says every record is exactly {line_length} bytes: "
                    f"{data_length} bytes of data followed by CR/LF. Fields after the extra or missing "
                    f"byte(s) are shifted, so other errors reported on this line may be caused by this one."
                ),
            ))
    return issues
