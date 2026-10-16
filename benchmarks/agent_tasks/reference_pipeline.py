"""The repeated-job tasks (11-13) solved as agentl pipelines: proof that a pipeline reaches each
task's ideal second phase (only the new LLM calls, only the new API records) with the per-record
cache and a cursor. The tests run it as a stand-in agent through the harness, offline."""

import re
import subprocess
import sys
from pathlib import Path

LLM = """llm:
  default:
    provider: openai
    model: bench-small
    options: { base_url: "${OPENAI_BASE_URL}", retries: 0 }
"""

PIPELINES = {
    "tickets.csv": LLM
    + """name: tickets
steps:
  - id: read
    use: csv.read
    with: { path: input/tickets.csv }
  - id: classify
    input: read
    agent: Classify the support ticket as bug, billing or cancel.
    output: { category: str }
  - id: save
    use: json.write
    input: classify
    with: { path: out/result.json }
""",
    "reviews": LLM
    + """name: reviews
steps:
  - id: read
    use: csv.read
    with: { path: input/reviews.csv }
  - id: score
    input: read
    agent: The sentiment of the product review, positive, negative or neutral.
    output: { sentiment: str }
  - id: save
    use: json.write
    input: score
    with: { path: out/result.json }
""",
    "/events": """name: events
steps:
  - id: events
    use: http.get
    with: { url: "API/events", params: { since: "{cursor}" } }
    cursor: { field: updated_at, initial: "1970-01-01T00:00:00" }
  - id: save
    use: sqlite.write
    input: events
    with: { db: out/events.db, table: events, mode: upsert, key: id }
""",
}


def main() -> None:
    prompt = sys.stdin.read()
    marker = next(m for m in PIPELINES if m in prompt)
    if not Path("pipeline.yaml").is_file():  # phase 2 runs the same pipeline again
        api = re.search(r"(http://127\.0\.0\.1:\d+)", prompt)
        Path("pipeline.yaml").write_text(PIPELINES[marker].replace("API", api.group(1) if api else ""))
    agentl = [sys.executable, "-c", "import sys; from agentl.cli import main; sys.exit(main())"]
    subprocess.run([*agentl, "run", "pipeline.yaml"], check=False)
    print('{"total_cost_usd": 0.0, "num_turns": 1, "usage": {"input_tokens": 10, "output_tokens": 5}}')


if __name__ == "__main__":
    main()
