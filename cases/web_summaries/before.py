"""Web page summaries, by hand: strip each page to its main text, ask the model for a summary,
save JSON."""

import glob
import json

import trafilatura

from cases.fake_llm import complete  # stands in for client.messages.create(...) plus parsing the answer


def main(work: str) -> None:
    out = []
    for path in sorted(glob.glob(f"{work}/data/pages/*.html")):
        with open(path, encoding="utf-8") as f:
            html = f.read()
        text = trafilatura.extract(
            html,
            output_format="markdown",
            include_tables=True,
            include_links=True,
            include_formatting=True,
            favor_recall=True,
        )
        if not text:
            continue
        meta = trafilatura.extract_metadata(html)
        out.append({"source": path, "title": meta.title if meta else "", **complete("summary", text.strip())})
    with open(f"{work}/out/summaries.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
