"""The docs banner names the latest release in CHANGELOG.md, so it cannot fall behind again."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_docs_banner_is_the_latest_release():
    latest = re.search(r"^## \[(\d+)\.(\d+)\.\d+\]", (ROOT / "CHANGELOG.md").read_text(), re.M)
    label = re.search(r"^\s+version_label: (\S+)", (ROOT / "mkdocs.yml").read_text(), re.M)
    assert latest and label
    assert label.group(1) == f"v{latest.group(1)}.{latest.group(2)}", (
        "update extra.version_label in mkdocs.yml"
    )
