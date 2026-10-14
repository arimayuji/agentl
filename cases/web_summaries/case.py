"""Turn web pages into clean text and summarize each one with an LLM."""

import json
from pathlib import Path

TITLE = "Web pages to summaries"
NEEDS: tuple[str, ...] = ("trafilatura",)
OUTPUT = "out/summaries.json"

TOPICS = ["Fuel prices", "Rain in Rio", "Port strike", "Coffee harvest", "Metro expansion", "Power outage"]


def make_data(work: Path) -> None:
    (work / "data/pages").mkdir()
    for i, topic in enumerate(TOPICS):
        body = " ".join(
            f"{topic} paragraph {j} with enough words to count as the main content of the page."
            for j in range(4)
        )
        (work / f"data/pages/p{i}.html").write_text(
            f"<html><head><title>{topic}</title></head><body><nav><a href='/'>Home</a> | About</nav>"
            f"<main><h1>{topic} this week</h1><p>{body}</p><p>{body}</p></main>"
            "<footer>Cookie settings. Privacy policy.</footer></body></html>",
            encoding="utf-8",
        )


def result(work: Path) -> list:
    rows = json.loads((work / OUTPUT).read_text(encoding="utf-8"))
    return sorted((Path(r["source"]).name, r["title"], r["summary"]) for r in rows)
