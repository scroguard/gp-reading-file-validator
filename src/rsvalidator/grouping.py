"""How a validated file is divided into report sections."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Grouping:
    line_count: int = 0
    routes: list[tuple[str, int]] = field(default_factory=list)  # (title, first line)
    accounts: list[tuple[str, int]] = field(default_factory=list)  # (title, first line)
    # line number -> ("account" | "route" | "file", index); issues on other lines go to the file section
    owner: dict[int, tuple[str, int]] = field(default_factory=dict)
