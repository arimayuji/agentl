"""Build CHANGELOG.md release sections from the fragments in ``changes/``.

Each pull request adds ``changes/<short-slug>.<section>.md`` holding the text of one bullet. At
release time ``build`` turns them into a ``## [X.Y.Z] - date`` section and deletes them.

    python tools/changelog.py preview
    python tools/changelog.py build --version 0.13.0 [--date 2026-10-18]

Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECTIONS = ["added", "changed", "fixed", "removed", "security", "docs"]
UNRELEASED = "## [Unreleased]"
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
FRAGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]*\.(?P<section>[a-z]+)\.md$")
UNRELEASED_LINK = re.compile(
    r"^\[Unreleased\]: (?P<base>\S+/compare/)v(?P<prev>\d+\.\d+\.\d+)\.\.\.HEAD$", re.M
)


class ChangelogError(Exception):
    pass


def _bullet(text: str) -> str:
    """Fragment text as a markdown bullet; continuation lines are indented under it."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    if lines[0].startswith("- "):  # already written as one or more bullets
        return "\n".join(lines)
    rest = [("  " + line.lstrip()) if line.strip() else "" for line in lines[1:]]
    return "\n".join(["- " + lines[0], *rest])


def collect(changes: Path) -> dict[str, list[tuple[Path, str]]]:
    """Fragments grouped by section, each group sorted by file name. Raises on bad names."""
    groups: dict[str, list[tuple[Path, str]]] = {s: [] for s in SECTIONS}
    bad = []
    for path in sorted(changes.glob("*.md")) if changes.is_dir() else []:
        if path.name == "README.md":
            continue
        m = FRAGMENT.match(path.name)
        if not m or m.group("section") not in SECTIONS:
            bad.append(path.name)
            continue
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            bad.append(f"{path.name} (empty)")
            continue
        groups[m.group("section")].append((path, _bullet(text)))
    if bad:
        raise ChangelogError(
            "bad fragment(s): " + ", ".join(bad) + f"; expected <slug>.<{'|'.join(SECTIONS)}>.md with text"
        )
    return groups


def render(groups: dict[str, list[tuple[Path, str]]]) -> str:
    parts = []
    for section in SECTIONS:
        if groups[section]:
            title = section.capitalize()
            parts.append(f"### {title}\n\n" + "\n".join(b for _, b in groups[section]) + "\n")
    return "\n".join(parts)


def build(root: Path, version: str, date: str) -> str:
    """Write the release section and links into CHANGELOG.md, then delete the fragments."""
    if not VERSION.match(version):
        raise ChangelogError(f"version must be X.Y.Z, got {version!r}")
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise ChangelogError(f"date must be YYYY-MM-DD, got {date!r}") from None
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    if re.search(rf"^## \[{re.escape(version)}\]", text, re.M):
        raise ChangelogError(f"CHANGELOG.md already has a [{version}] section; nothing changed")
    groups = collect(root / "changes")
    if not any(groups.values()):
        raise ChangelogError("no fragments in changes/; nothing to release")
    if text.count(UNRELEASED) != 1:
        raise ChangelogError(f"CHANGELOG.md must have exactly one '{UNRELEASED}' heading")
    link = UNRELEASED_LINK.search(text)
    if not link:
        raise ChangelogError("CHANGELOG.md has no '[Unreleased]: .../compare/vX.Y.Z...HEAD' link")

    # The release section goes right before the first older release heading, after the note.
    start = text.index(UNRELEASED)
    nxt = re.compile(r"^## \[", re.M).search(text, start + len(UNRELEASED))
    if not nxt:
        raise ChangelogError("CHANGELOG.md has no release section after [Unreleased]")
    section = f"## [{version}] - {date}\n\n{render(groups)}\n"
    text = text[: nxt.start()] + section + text[nxt.start() :]

    base, prev = link.group("base"), link.group("prev")
    text = UNRELEASED_LINK.sub(
        f"[Unreleased]: {base}v{version}...HEAD\n[{version}]: {base}v{prev}...v{version}", text, count=1
    )
    tmp = changelog.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(changelog)
    for paths in groups.values():
        for path, _ in paths:
            path.unlink()
    return section


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="write the release section and delete the fragments")
    b.add_argument("--version", required=True)
    b.add_argument("--date", default=dt.date.today().isoformat())
    sub.add_parser("preview", help="print the entries the next build would add")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "preview":
            groups = collect(args.root / "changes")
            print(render(groups) if any(groups.values()) else "no fragments in changes/", end="\n")
        else:
            print(build(args.root, args.version, args.date), end="")
    except ChangelogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
