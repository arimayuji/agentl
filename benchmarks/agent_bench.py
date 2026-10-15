"""The same ETL tasks given to a coding agent with agentl and without it.

    python -m benchmarks.agent_bench run --arm both --runs 3
    python -m benchmarks.agent_bench report benchmarks/agent-results.jsonl

Each task in `benchmarks/agent_tasks/<task>/` has `task.md` (the request), `input/` and
`expected.json` (the records the output must hold). A run copies the inputs to an empty folder,
gives the agent the request, then compares the output it wrote with the expected records. An HTTP
server on 127.0.0.1 serves `input/api/` for tasks that call an API.

- arm `agentl`: the agentl skill is installed in the folder and the request says to use agentl.
- arm `plain`: the request says to use Python without agentl.

The agent is any command that reads the request on stdin and works in the current folder; the
default is Claude Code headless. Its JSON output, when present, gives cost, tokens and turns.
Runs cost money and vary from run to run: report every run, the spread and the model."""

from __future__ import annotations

import argparse
import csv
import http.server
import json
import os
import shlex
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

TASKS = Path(__file__).parent / "agent_tasks"
DEFAULT_AGENT = "claude -p --output-format json --allowedTools Bash,Read,Write,Edit,Glob,Grep"
ARMS = {
    "agentl": (
        "Use agentl for this (it is installed: `agentl --help`, docs at https://agentl.com.br/llms.txt). "
        "Write the pipeline as pipeline.yaml in this folder and run it.\n\n"
    ),
    "plain": "Use Python (the standard library, pandas or any installed package) but not agentl.\n\n",
}


# ---------------------------------------------------------------- scoring
def _norm(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    try:
        return round(float(str(value).replace(",", "")), 6)
    except ValueError:
        return str(value).strip()


def _key(record: dict, key: str) -> tuple:
    return tuple(_norm(record.get(k)) for k in key.split("+"))


def read_output(path: Path) -> list[dict]:
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else [data]
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def score(task: Path, workspace: Path) -> dict:
    """Records matched by key; a matched record is right when every expected field is equal
    (numbers compared as numbers, empty and null alike). Extra fields are ignored."""
    spec = json.loads((task / "expected.json").read_text(encoding="utf-8"))
    out = workspace / spec["output"]
    if not out.is_file():
        return {"ok": False, "detail": f"no {spec['output']}", "right": 0, "expected": len(spec["records"])}
    try:
        got = read_output(out)
    except Exception as e:
        return {
            "ok": False,
            "detail": f"unreadable output: {e}",
            "right": 0,
            "expected": len(spec["records"]),
        }
    by_key = {_key(r, spec["key"]): r for r in got if isinstance(r, dict)}
    right, wrong = 0, []
    for exp in spec["records"]:
        rec = by_key.get(_key(exp, spec["key"]))
        if rec is not None and all(_norm(rec.get(k)) == _norm(v) for k, v in exp.items()):
            right += 1
        else:
            wrong.append(_key(exp, spec["key"]))
    ok = right == len(spec["records"]) and len(got) == len(spec["records"])
    detail = "" if ok else f"wrong or missing {wrong[:3]}, {len(got)} rows written"
    return {
        "ok": ok,
        "detail": detail,
        "right": right,
        "expected": len(spec["records"]),
    }


# ---------------------------------------------------------------- one run
class _Api:
    def __init__(self, root: Path):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                url = urlparse(self.path)
                page = parse_qs(url.query).get("page", ["1"])[0]
                name = url.path.strip("/")
                path = root / f"{name}.page{page}.json"
                path = path if path.is_file() else root / f"{name}.json"
                body = path.read_bytes() if path.is_file() else b""
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def _metrics(stdout: str) -> dict:
    """Cost, tokens and turns from Claude Code's `--output-format json`; empty for other agents."""
    try:
        data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        return {}
    if not isinstance(data, dict):
        return {}
    usage = data.get("usage") or {}
    tokens = sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    return {
        "cost_usd": data.get("total_cost_usd"),
        "turns": data.get("num_turns"),
        "tokens": tokens or None,
        "agent_error": bool(data.get("is_error")),
    }


def _argv(command: str) -> list[str]:
    """Split the agent command like the shell would, on Windows too (backslashes in paths)."""
    if os.name != "nt":
        return shlex.split(command)
    return [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in shlex.split(command, posix=False)]


def run_one(task: Path, arm: str, agent: str, timeout: float) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"agentl-bench-{task.name}-") as tmp:
        ws = Path(tmp)
        shutil.copytree(task / "input", ws / "input")
        api = _Api(ws / "input" / "api") if (ws / "input" / "api").is_dir() else None
        if arm == "agentl":
            skill = Path(__file__).parent.parent / "agentl" / "skill" / "SKILL.md"
            (ws / ".claude" / "skills" / "agentl").mkdir(parents=True)
            shutil.copy(skill, ws / ".claude" / "skills" / "agentl" / "SKILL.md")
        prompt = ARMS[arm] + (task / "task.md").read_text(encoding="utf-8")
        if api:
            prompt = prompt.replace("{api_url}", api.url)
        t0 = time.time()
        try:
            proc = subprocess.run(
                _argv(agent), input=prompt, cwd=ws, capture_output=True, text=True, timeout=timeout
            )
            stdout, timed_out = proc.stdout, False
        except subprocess.TimeoutExpired as e:
            stdout, timed_out = (
                (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                True,
            )
        seconds = round(time.time() - t0, 1)
        if api:
            api.close()
        result = score(task, ws)
        return {
            "task": task.name,
            "arm": arm,
            **result,
            "seconds": seconds,
            "timed_out": timed_out,
            **_metrics(stdout),
        }


# ---------------------------------------------------------------- report
def _spread(values: list[Any]) -> str:
    values = sorted(v for v in values if v is not None)
    if not values:
        return "n/a"
    med = statistics.median(values)
    return f"{med:,.3g} ({values[0]:,.3g}-{values[-1]:,.3g})"


def report(rows: list[dict]) -> str:
    lines = [
        "| arm | runs | success | cost USD median (min-max) | tokens | turns | seconds |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == arm]
        ok = sum(r["ok"] for r in mine)
        cols = [_spread([r.get(k) for r in mine]) for k in ("cost_usd", "tokens", "turns", "seconds")]
        lines.append(
            f"| {arm} | {len(mine)} | {ok}/{len(mine)} ({ok / len(mine):.0%}) | " + " | ".join(cols) + " |"
        )
    lines += [
        "",
        "| task | " + " | ".join(sorted({r["arm"] for r in rows})) + " |",
        "|---|" + "---|" * len({r["arm"] for r in rows}),
    ]
    for task in sorted({r["task"] for r in rows}):
        cells = []
        for arm in sorted({r["arm"] for r in rows}):
            mine = [r for r in rows if r["task"] == task and r["arm"] == arm]
            cells.append(f"{sum(r['ok'] for r in mine)}/{len(mine)}")
        lines.append(f"| {task} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks.agent_bench")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--arm", choices=["agentl", "plain", "both"], default="both")
    r.add_argument("--runs", type=int, default=3)
    r.add_argument("--tasks", nargs="*", help="task names (default: all)")
    r.add_argument("--agent", default=DEFAULT_AGENT, help="command that reads the request on stdin")
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--out", default="benchmarks/agent-results.jsonl")
    rep = sub.add_parser("report")
    rep.add_argument("results")
    a = p.parse_args(argv)
    if a.cmd == "report":
        rows = [json.loads(line) for line in Path(a.results).read_text().splitlines() if line.strip()]
        print(report(rows))
        return 0
    tasks = sorted(t for t in TASKS.iterdir() if t.is_dir() and (not a.tasks or t.name in a.tasks))
    arms = ["agentl", "plain"] if a.arm == "both" else [a.arm]
    rows = []
    with open(a.out, "a", encoding="utf-8") as out:
        for n in range(a.runs):
            for task in tasks:
                for arm in arms:
                    row = {"run": n + 1, "agent": a.agent, **run_one(task, arm, a.agent, a.timeout)}
                    rows.append(row)
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    print(f"{task.name:<32} {arm:<7} {'ok' if row['ok'] else 'FAIL'} {row.get('detail', '')}")
    print()
    print(report(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
