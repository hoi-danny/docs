#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$")
HUNK_RE = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_len>\d+))?\s+\+(?P<new_start>\d+)(?:,(?P<new_len>\d+))?\s+@@"
)


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: List[str]  # raw diff lines after @@ header (until next @@/file header)


def run_git(args: List[str]) -> str:
    p = subprocess.run(
        ["git", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{p.stderr}".strip())
    return p.stdout


def list_changed_files(base: str, head: str) -> List[str]:
    out = run_git(["diff", "--name-only", f"{base}...{head}"])
    files = [line.strip() for line in out.splitlines() if line.strip()]
    return files


def load_file_at_rev(path: str, rev: str) -> Optional[str]:
    try:
        return run_git(["show", f"{rev}:{path}"])
    except Exception:
        return None


def headings_by_line(text: str) -> Dict[int, str]:
    """
    Returns mapping line_number(1-indexed) -> section title string, for heading lines only.
    """
    mapping: Dict[int, str] = {}
    for i, line in enumerate(text.splitlines(), start=1):
        m = HEADING_RE.match(line)
        if m:
            title = m.group("title").strip()
            level = len(m.group("level"))
            mapping[i] = f"{'#' * level} {title}"
    return mapping


def section_for_line(heading_lines: Dict[int, str], line_no: int) -> str:
    if not heading_lines:
        return "(no headings)"
    prior = [ln for ln in heading_lines.keys() if ln <= line_no]
    if not prior:
        # before first heading
        first_ln = min(heading_lines.keys())
        return heading_lines[first_ln]
    return heading_lines[max(prior)]


def parse_unified_diff(diff_text: str) -> List[Hunk]:
    hunks: List[Hunk] = []
    cur: Optional[Hunk] = None

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            continue
        if raw.startswith("--- ") or raw.startswith("+++ "):
            continue
        if raw.startswith("@@ "):
            m = HUNK_RE.match(raw)
            if not m:
                continue
            if cur is not None:
                hunks.append(cur)
            cur = Hunk(
                old_start=int(m.group("old_start")),
                old_len=int(m.group("old_len") or "1"),
                new_start=int(m.group("new_start")),
                new_len=int(m.group("new_len") or "1"),
                lines=[],
            )
            continue
        if cur is not None:
            cur.lines.append(raw)

    if cur is not None:
        hunks.append(cur)
    return hunks


@dataclass
class SectionStats:
    plus: int = 0
    minus: int = 0
    first_seen_index: int = 10**9
    samples_plus: List[str] = None  # type: ignore[assignment]
    samples_minus: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.samples_plus is None:
            self.samples_plus = []
        if self.samples_minus is None:
            self.samples_minus = []


def summarize_file(
    path: str,
    base: str,
    head: str,
    *,
    max_samples_per_section: int = 6,
    max_sections_per_file: int = 60,
) -> Optional[str]:
    new_text = load_file_at_rev(path, head)
    old_text = load_file_at_rev(path, base)

    # Skip if binary / missing in both (unlikely)
    if new_text is None and old_text is None:
        return None

    new_headings = headings_by_line(new_text or "")
    old_headings = headings_by_line(old_text or "")

    diff = run_git(["diff", "--unified=0", f"{base}...{head}", "--", path])
    if not diff.strip():
        return None

    hunks = parse_unified_diff(diff)
    if not hunks:
        return None

    sections: Dict[str, SectionStats] = {}
    seen_counter = 0

    for h in hunks:
        old_ln = h.old_start
        new_ln = h.new_start
        for line in h.lines:
            if not line:
                continue
            tag = line[0]
            content = line[1:] if len(line) > 1 else ""

            if tag == "+" and not line.startswith("+++"):
                sec = section_for_line(new_headings, new_ln)
                st = sections.setdefault(sec, SectionStats())
                if st.first_seen_index == 10**9:
                    st.first_seen_index = seen_counter
                    seen_counter += 1
                st.plus += 1
                if content.strip() and len(st.samples_plus) < max_samples_per_section:
                    st.samples_plus.append(content.rstrip())
                new_ln += 1
            elif tag == "-" and not line.startswith("---"):
                sec = section_for_line(old_headings, old_ln)
                st = sections.setdefault(sec, SectionStats())
                if st.first_seen_index == 10**9:
                    st.first_seen_index = seen_counter
                    seen_counter += 1
                st.minus += 1
                if content.strip() and len(st.samples_minus) < max_samples_per_section:
                    st.samples_minus.append(content.rstrip())
                old_ln += 1
            else:
                # context line (shouldn't exist with --unified=0, but handle anyway)
                if tag == " ":
                    old_ln += 1
                    new_ln += 1

    if not sections:
        return None

    lines: List[str] = []
    lines.append(f"### `{path}`")
    ordered = sorted(sections.items(), key=lambda kv: kv[1].first_seen_index)
    if max_sections_per_file > 0 and len(ordered) > max_sections_per_file:
        ordered = ordered[:max_sections_per_file]
        truncated = True
    else:
        truncated = False

    for sec, st in ordered:
        lines.append(f"- **{sec}**: `+{st.plus} / -{st.minus}`")
        if st.samples_plus:
            for s in st.samples_plus:
                lines.append(f"  - `+` {s}")
        if st.samples_minus:
            for s in st.samples_minus:
                lines.append(f"  - `-` {s}")
    if truncated:
        lines.append(f"- _(truncated)_ 보여준 섹션 수가 `{max_sections_per_file}`를 초과해 일부 섹션은 생략되었습니다.")
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    base = os.environ.get("BASE_SHA")
    head = os.environ.get("HEAD_SHA")
    only_globs = os.environ.get("ONLY_GLOBS", "").strip()
    max_sections_per_file = int(os.environ.get("MAX_SECTIONS_PER_FILE", "60").strip() or "60")

    if not base or not head:
        sys.stderr.write("BASE_SHA and HEAD_SHA env vars are required.\n")
        return 2

    patterns: List[str] = []
    if only_globs:
        patterns = [p.strip() for p in only_globs.split(",") if p.strip()]

    changed = list_changed_files(base, head)
    if patterns:
        import fnmatch

        changed = [p for p in changed if any(fnmatch.fnmatch(p, pat) for pat in patterns)]

    if not changed:
        print("## Section-level change summary\n\nNo changed files.\n")
        return 0

    out_lines: List[str] = []
    out_lines.append("## Section-level change summary")
    out_lines.append("")
    out_lines.append(f"Base: `{base}`  \nHead: `{head}`")
    out_lines.append("")

    any_summaries = 0
    for path in changed:
        # Focus on markdown-like docs, but keep a reasonable default
        if not re.search(r"\.(md|mdx|markdown|txt)$", path, re.IGNORECASE):
            continue
        summary = summarize_file(path, base, head, max_sections_per_file=max_sections_per_file)
        if summary:
            out_lines.append(summary)
            out_lines.append("")
            any_summaries += 1

    if any_summaries == 0:
        out_lines.append("No markdown/text section changes detected (or files were non-text).")
        out_lines.append("")

    print("\n".join(out_lines).rstrip() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

