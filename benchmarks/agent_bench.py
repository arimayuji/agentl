"""The same ETL tasks given to a coding agent with agentl and without it.

    python -m benchmarks.agent_bench run --arm both --runs 3
    python -m benchmarks.agent_bench report benchmarks/agent-results.jsonl

Each task in `benchmarks/agent_tasks/<task>/` has `task.md` (the request), `input/` and
`expected.json` (the records the output must hold). A run copies the inputs to an empty folder,
gives the agent the request, then compares the output it wrote with the expected records. An HTTP
server on 127.0.0.1 serves `input/api/` for tasks that call an API.

- arm `agentl`: the agentl skill is installed in the folder and the request says to use agentl;
  the agent picks a script or a pipeline.
- arm `agentl-pipeline`: the same, but the request asks for a pipeline.
- arm `agentl-python`: the request asks for a script with agentl's Python API.
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
import re
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
    # The agent picks the size (script or pipeline) as the skill says, like a real user's agent.
    "agentl": "Use agentl for this (it is installed; the agentl skill explains how).\n\n",
    # Forced pipeline: what a pipeline costs on one-off tasks, and what it saves on repeated ones.
    "agentl-pipeline": (
        "Use agentl for this (it is installed; the agentl skill explains how). "
        "Write it as a pipeline, pipeline.yaml in this folder, and run it with `agentl run`.\n\n"
    ),
    "agentl-python": (
        "Use agentl as a Python library for this (it is installed): a short script with "
        "`import agentl as al` and al.read, al.write, al.http_get_all, then plain Python. No pipeline.\n\n"
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


def read_output(path: Path, table: str | None = None) -> list[dict]:
    if path.suffix == ".db":
        import sqlite3

        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(f'select * from "{table}"')]  # table from expected.json
        finally:
            con.close()
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
        got = read_output(out, spec.get("table"))
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
    """Serves `input/api/<name>.json` (or `<name>.page<N>.json` for `?page=N`). A list served with
    `?since=X` keeps the items whose `updated_at` is after X. Counts the items served."""

    def __init__(self, root: Path):
        api = self
        self.served = 0

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                url = urlparse(self.path)
                query = parse_qs(url.query)
                page = query.get("page", ["1"])[0]
                name = url.path.strip("/")
                path = root / f"{name}.page{page}.json"
                path = path if path.is_file() else root / f"{name}.json"
                body = path.read_bytes() if path.is_file() else b""
                if body:
                    data = json.loads(body)
                    if isinstance(data, list) and query.get("since"):
                        since = query["since"][0]
                        data = [d for d in data if str(d.get("updated_at", "")) > since]
                        body = json.dumps(data).encode()
                    items = data if isinstance(data, list) else data.get("data", [])
                    api.served += len(items) if isinstance(items, list) else 0
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


_ID = re.compile(r"\b[A-Z]{1,3}-\d{2,}\b")


class _Llm:
    """An OpenAI-compatible chat API on 127.0.0.1 that knows the answers: it finds the record id
    (like `T-001`) in the message and answers that record's fields from `answers`, as JSON in the
    shape the request's json_schema asks for (or plain JSON). `fail_after` calls, it answers 503
    (a provider outage). Counts calls, so a run that pays twice for the same record shows."""

    def __init__(self, answers: dict[str, dict], fail_after: int | None = None):
        llm = self
        self.calls = 0
        self.fail_after = fail_after
        self.lock = threading.Lock()

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:  # /models, for clients that list them first
                self._send(200, {"data": [{"id": "bench-small", "object": "model"}]})

            def do_POST(self) -> None:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                with llm.lock:
                    llm.calls += 1
                    down = llm.fail_after is not None and llm.calls > llm.fail_after
                if down:
                    self._send(503, {"error": {"message": "the provider is overloaded, try later"}})
                    return
                text = " ".join(
                    m["content"] if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
                    for m in req.get("messages", [])
                    if m.get("role") == "user"
                )
                found = [i for i in dict.fromkeys(_ID.findall(text)) if i in answers]
                answer = dict(answers[found[0]]) if found else {}
                fmt = ((req.get("response_format") or {}).get("json_schema") or {}).get("schema") or {}
                props = fmt.get("properties") or {}
                if props:  # fill the schema: known fields from the answer, typed blanks for the rest
                    blank = {
                        "string": "",
                        "boolean": False,
                        "number": 0,
                        "integer": 0,
                        "array": [],
                        "object": {},
                    }
                    out = {}
                    for name, p in props.items():
                        kind = p.get("type")
                        kind = (
                            next((t for t in kind if t != "null"), "string")
                            if isinstance(kind, list)
                            else kind
                        )
                        out[name] = answer.get(name, blank.get(kind or "string", ""))
                    answer = out
                self._send(
                    200,
                    {
                        "id": f"bench-{llm.calls}",
                        "object": "chat.completion",
                        "model": req.get("model", "bench-small"),
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": json.dumps(answer)},
                            }
                        ],
                        "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
                    },
                )

            def _send(self, code: int, data: dict) -> None:
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

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
    # Without cache reads: every turn re-reads the conversation from the cache, which inflates
    # `tokens` with turns; cost already weighs cache reads at their (lower) price.
    fresh = sum(
        int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens")
    )
    return {
        "cost_usd": data.get("total_cost_usd"),
        "turns": data.get("num_turns"),
        "tokens": tokens or None,
        "fresh_tokens": fresh or None,
        "agent_error": bool(data.get("is_error")) or data.get("subtype") not in (None, "success"),
        "agent_says": str(data.get("result") or "")[:300],
        "models": sorted(data.get("modelUsage") or {}),  # which model(s) did the work
    }


def _argv(command: str) -> list[str]:
    """Split the agent command like the shell would, on Windows too (backslashes in paths)."""
    if os.name != "nt":
        return shlex.split(command)
    return [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in shlex.split(command, posix=False)]


def _invoke(
    agent: str, prompt: str, ws: Path, timeout: float, env: dict
) -> tuple[str, str, int | None, bool]:
    try:
        proc = subprocess.run(
            _argv(agent), input=prompt, cwd=ws, capture_output=True, text=True, timeout=timeout, env=env
        )
        return proc.stdout, proc.stderr, proc.returncode, False
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return out, "", None, True
    except FileNotFoundError as e:
        return "", f"agent command not found: {e}", 127, False


def _add(total: dict, part: dict) -> dict:
    """Metrics of two phases added up (cost, turns, tokens); the rest from the last phase."""
    out = {**total, **part}
    for k in ("cost_usd", "turns", "tokens", "fresh_tokens"):
        if total.get(k) is not None or part.get(k) is not None:
            out[k] = (total.get(k) or 0) + (part.get(k) or 0)
    return out


def run_one(task: Path, arm: str, agent: str, timeout: float, keep: Path | None = None) -> dict:
    """One task, one arm. A task with `task2.md` runs twice in the same folder, like the next day:
    `input2/` is copied over `input/` first. `bench.json` may add a fake LLM (`llm`) and says what
    the second phase should cost (`measure`: LLM calls or API records, with its `ideal`)."""
    cfg = (
        json.loads((task / "bench.json").read_text(encoding="utf-8"))
        if (task / "bench.json").is_file()
        else {}
    )
    with tempfile.TemporaryDirectory(prefix=f"agentl-bench-{task.name}-") as tmp:
        ws = Path(tmp)
        shutil.copytree(task / "input", ws / "input")
        api = _Api(ws / "input" / "api") if (ws / "input" / "api").is_dir() else None
        llm = None
        if "llm" in cfg:
            spec = json.loads((task / "expected.json").read_text(encoding="utf-8"))
            key = spec["key"]
            answers = {str(r[key]): {k: v for k, v in r.items() if k != key} for r in spec["records"]}
            llm = _Llm(answers, cfg["llm"].get("fail_after"))
        if arm.startswith("agentl"):
            skill = Path(__file__).parent.parent / "agentl" / "skill" / "SKILL.md"
            (ws / ".claude" / "skills" / "agentl").mkdir(parents=True)
            shutil.copy(skill, ws / ".claude" / "skills" / "agentl" / "SKILL.md")

        def prompt_for(name: str) -> str:
            text = ARMS[arm] + (task / name).read_text(encoding="utf-8")
            if api:
                text = text.replace("{api_url}", api.url)
            if llm:
                text = text.replace("{llm_url}", llm.url)
            return text

        # The local API stands in for a public one: let agentl's SSRF guard reach it, as it would
        # reach the real API, so no arm spends turns on the guard.
        env = {**os.environ, "AGENTL_ALLOW_HOSTS": "127.0.0.1"}
        if llm:
            env.update(OPENAI_API_KEY="bench", OPENAI_BASE_URL=llm.url)
        phases = ["task.md"] + (["task2.md"] if (task / "task2.md").is_file() else [])
        t0 = time.time()
        metrics: dict = {}
        stdout = stderr = ""
        code: int | None = 0
        timed_out = False
        second: dict[str, int] = {}
        for n, name in enumerate(phases):
            if n == 1:
                if (task / "input2").is_dir():
                    shutil.copytree(task / "input2", ws / "input", dirs_exist_ok=True)
                if llm:
                    llm.fail_after = None  # the provider is back
                calls0, served0 = (llm.calls if llm else 0), (api.served if api else 0)
            stdout, stderr, code, timed_out = _invoke(agent, prompt_for(name), ws, timeout, env)
            metrics = _add(metrics, _metrics(stdout))
            if keep is not None:  # what the agent wrote and said, to see where its turns went
                keep.mkdir(parents=True, exist_ok=True)
                (keep / f"agent-stdout{'' if n == 0 else n + 1}.txt").write_text(stdout, encoding="utf-8")
            if n == 1:
                second = {
                    "phase2_llm_calls": (llm.calls - calls0) if llm else 0,
                    "phase2_api_records": (api.served - served0) if api else 0,
                }
            if timed_out or code == 127:  # a failed phase 1 (an outage) still gets its next day
                break
        seconds = round(time.time() - t0, 1)
        if keep is not None:
            shutil.copytree(ws, keep / "workspace", ignore=shutil.ignore_patterns("input", ".claude"))
        for server in (api, llm):
            if server:
                server.close()
        result = score(task, ws)
        measure = cfg.get("measure")
        if measure and second:
            got = second[f"phase2_{measure['what']}"]
            result["phase2"] = got
            result["phase2_ideal"] = measure["ideal"]
        return {
            "task": task.name,
            "arm": arm,
            **result,
            **second,
            "seconds": seconds,
            "timed_out": timed_out,
            "agent_exit": code,
            **metrics,
            # what the agent said when it ended, to tell "agent failed to start" from "wrong answer"
            "agent_tail": (stderr.strip() or stdout.strip())[-300:] if not result["ok"] else "",
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
        "| arm | runs | success | cost USD median (min-max) | turns | seconds | tokens without cache reads |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == arm]
        ok = sum(r["ok"] for r in mine)
        cols = [_spread([r.get(k) for r in mine]) for k in ("cost_usd", "turns", "seconds", "fresh_tokens")]
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
    repeated = sorted({r["task"] for r in rows if r.get("phase2_ideal") is not None})
    if repeated:  # jobs that run twice: what the second run paid for, against the ideal
        arms = sorted({r["arm"] for r in rows})
        lines += [
            "",
            "Second run (LLM calls or API records paid; ideal = only what is new):",
            "",
            "| task | ideal | " + " | ".join(arms) + " |",
            "|---|---|" + "---|" * len(arms),
        ]
        for task in repeated:
            mine = [r for r in rows if r["task"] == task]
            cells = [_spread([r.get("phase2") for r in mine if r["arm"] == arm]) for arm in arms]
            lines.append(f"| {task} | {mine[0]['phase2_ideal']} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _agent_broken(row: dict) -> bool:
    """The agent itself failed (could not start, not logged in, refused), not the task."""
    return not row["ok"] and (
        row.get("agent_error") or row.get("agent_exit") not in (0, None) or (row.get("turns") or 0) <= 1
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks.agent_bench")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--arm", choices=[*ARMS, "both", "all"], default="both", help="both = agentl and plain")
    r.add_argument("--runs", type=int, default=3)
    r.add_argument("--tasks", nargs="*", help="task names (default: all)")
    r.add_argument("--agent", default=DEFAULT_AGENT, help="command that reads the request on stdin")
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--out", default="benchmarks/agent-results.jsonl")
    r.add_argument(
        "--keep",
        metavar="DIR",
        help="keep each run's files and the agent's output here (with `--output-format stream-json "
        "--verbose` in the agent command, the whole transcript)",
    )
    rep = sub.add_parser("report")
    rep.add_argument("results")
    a = p.parse_args(argv)
    if a.cmd == "report":
        rows = [json.loads(line) for line in Path(a.results).read_text().splitlines() if line.strip()]
        print(report(rows))
        return 0
    tasks = sorted(
        t for t in TASKS.iterdir() if (t / "task.md").is_file() and (not a.tasks or t.name in a.tasks)
    )
    arms = {"both": ["agentl", "plain"], "all": list(ARMS)}.get(a.arm, [a.arm])

    def keep(n: int, task: Path, arm: str) -> Path | None:
        return Path(a.keep) / f"run{n + 1}-{task.name}-{arm}" if a.keep else None

    rows = []
    with open(a.out, "a", encoding="utf-8") as out:
        for n in range(a.runs):
            for task in tasks:
                for arm in arms:
                    row = {
                        "run": n + 1,
                        "agent": a.agent,
                        **run_one(task, arm, a.agent, a.timeout, keep(n, task, arm)),
                    }
                    rows.append(row)
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    print(f"{task.name:<32} {arm:<7} {'ok' if row['ok'] else 'FAIL'} {row.get('detail', '')}")
                    if not row["ok"] and row.get("agent_tail"):
                        print(f"    agent: {row['agent_tail'][-200:]}")
                    if len(rows) >= 2 and all(_agent_broken(r) for r in rows):
                        print(
                            "\nstopped: the agent did not work on the first runs (it ended at once with "
                            "an error or no turns). Check the command alone, e.g.\n"
                            "  echo 'write the word hi to out/hi.txt' | " + a.agent
                        )
                        return 2
    print()
    print(report(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
