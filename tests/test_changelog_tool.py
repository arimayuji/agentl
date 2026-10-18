"""tools/changelog.py builds release sections from changes/ fragments."""

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("changelog_tool", ROOT / "tools" / "changelog.py")
assert _spec and _spec.loader
changelog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog)

BASE = "https://github.com/arimayuji/agentl/compare/"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    shutil.copy(ROOT / "CHANGELOG.md", tmp_path / "CHANGELOG.md")
    changes = tmp_path / "changes"
    changes.mkdir()
    (changes / "README.md").write_text("# not a fragment\n")
    (changes / "zeta.docs.md").write_text("Docs entry.\n")
    (changes / "b-fix.fixed.md").write_text("Second fix (#2).\n")
    (changes / "a-fix.fixed.md").write_text("First fix that wraps\nonto a second line (#1).\n")
    (changes / "new.added.md").write_text("New thing.\n")
    (changes / "sec.security.md").write_text("Security entry.\n")
    return tmp_path


def _prev(text: str) -> str:
    m = changelog.UNRELEASED_LINK.search(text)
    assert m
    return m.group("prev")


def test_build_groups_orders_links_and_deletes(repo: Path) -> None:
    before = (repo / "CHANGELOG.md").read_text()
    prev = _prev(before)
    assert changelog.main(["--root", str(repo), "build", "--version", "9.9.9", "--date", "2030-01-02"]) == 0
    text = (repo / "CHANGELOG.md").read_text()

    unreleased = text.index("## [Unreleased]")
    release = text.index("## [9.9.9] - 2030-01-02")
    assert unreleased < release < text.index(f"## [{prev}]")
    body = text[release : text.index(f"## [{prev}]")]
    heads = [line for line in body.splitlines() if line.startswith("### ")]
    assert heads == ["### Added", "### Fixed", "### Security", "### Docs"]
    assert body.index("- First fix that wraps\n  onto a second line (#1).") < body.index("- Second fix (#2).")

    assert f"[Unreleased]: {BASE}v9.9.9...HEAD\n[9.9.9]: {BASE}v{prev}...v9.9.9\n" in text
    assert f"[Unreleased]: {BASE}v{prev}...HEAD" not in text
    assert sorted(p.name for p in (repo / "changes").iterdir()) == ["README.md"]
    assert (
        text.replace(body, "")
        .replace(f"[9.9.9]: {BASE}v{prev}...v9.9.9\n", "")
        .replace("v9.9.9...HEAD", f"v{prev}...HEAD")
        == before
    )


def test_build_twice_fails_without_changes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--root", str(repo), "build", "--version", "9.9.9", "--date", "2030-01-02"]
    assert changelog.main(args) == 0
    after = (repo / "CHANGELOG.md").read_text()
    assert changelog.main(args) == 1
    assert "already has a [9.9.9] section" in capsys.readouterr().err
    assert (repo / "CHANGELOG.md").read_text() == after


def test_build_without_fragments_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for p in (repo / "changes").glob("*.*.md"):
        p.unlink()
    before = (repo / "CHANGELOG.md").read_text()
    assert changelog.main(["--root", str(repo), "build", "--version", "9.9.9"]) == 1
    assert "no fragments in changes/" in capsys.readouterr().err
    assert (repo / "CHANGELOG.md").read_text() == before


def test_bad_fragment_name_fails_and_keeps_files(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "changes" / "oops.feature.md").write_text("x\n")
    assert changelog.main(["--root", str(repo), "build", "--version", "9.9.9"]) == 1
    assert "oops.feature.md" in capsys.readouterr().err
    assert (repo / "changes" / "new.added.md").exists()


def test_bad_version_fails(repo: Path) -> None:
    assert changelog.main(["--root", str(repo), "build", "--version", "v1.0"]) == 1


def test_preview_prints_without_changing(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = (repo / "CHANGELOG.md").read_text()
    assert changelog.main(["--root", str(repo), "preview"]) == 0
    out = capsys.readouterr().out
    assert out.index("### Added") < out.index("### Fixed") < out.index("### Docs")
    assert (repo / "CHANGELOG.md").read_text() == before
    assert (repo / "changes" / "new.added.md").exists()


def test_repo_fragments_are_valid() -> None:
    changelog.collect(ROOT / "changes")
