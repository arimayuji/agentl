"""`agentl report`: a ready-to-paste bug report for a failed run or a CLI crash.

Everything is built locally and printed: version, Python and OS, the failing step with its error
and traceback, what `agentl doctor` sees, the pipeline YAML with secrets masked and a minimal
repro suggestion, plus a prefilled GitHub "new issue" URL. Nothing is sent over the network.
"""

from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from . import assist, secrets

REPO = "https://github.com/arimayuji/agentl"
TEMPLATE = "bug_report.yml"
CONTRIBUTING_AI = f"{REPO}/blob/main/.github/CONTRIBUTING_AI.md"
URL_LIMIT = 8000  # GitHub and browsers cut longer URLs
CRASH_FILE = ".agentl/last-crash.txt"
QUIET_ENV = "AGENTL_NO_REPORT_HINT"
NOTICE = "Nothing was sent: agentl report only prints. Review it before you share it."

# Exception types that usually mean a bug in agentl rather than in the pipeline or the data.
BUG_TYPES = (
    "TypeError",
    "AttributeError",
    "NameError",
    "UnboundLocalError",
    "AssertionError",
    "RecursionError",
    "NotImplementedError",
    "IndexError",
    "ZeroDivisionError",
)

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
_KEY_VALUE = re.compile(r"^(\s*-?\s*[\"']?)([A-Za-z0-9_.-]+)([\"']?\s*:\s*)(\S.*?)\s*$")


# ---------------------------------------------------------------- redaction
def mask_yaml(text: str) -> str:
    """The pipeline YAML safe to paste: `${VAR:-default}` loses its default, values of keys that look
    like secrets (token, password, dsn...) become `***` unless they are `${VAR}` references, and
    secret values this process knows (see `repair`) are masked everywhere."""
    from .repair import _hidden

    out = []
    for line in _VAR.sub(lambda m: "${" + m.group(1) + "}", text).splitlines():
        m = _KEY_VALUE.match(line)
        if m and secrets.SECRET_NAME.search(m.group(2)) and not m.group(4).startswith(("${", "{", "[")):
            line = f"{m.group(1)}{m.group(2)}{m.group(3)}***"
        out.append(line)
    masked = "\n".join(out) + ("\n" if text.endswith("\n") else "")
    return secrets.mask(masked, _hidden())


def _mask(text: str | None) -> str | None:
    from .repair import _hidden

    return secrets.mask(text, _hidden()) if text else text


# ---------------------------------------------------------------- classification
def likely_bug(error: str | None, step_kind: str | None = None) -> bool:
    """True when an error looks like an agentl bug: an internal exception type raised outside the
    user's own code (`fn:` steps are the user's code)."""
    if not error or step_kind == "fn":
        return False
    return error.split(":", 1)[0].strip() in BUG_TYPES


def hint(run_id: str | None = None) -> str | None:
    """The one line printed after an unexpected error, or None when silenced."""
    if os.environ.get(QUIET_ENV):
        return None
    target = f"agentl report {run_id}" if run_id else "agentl report --crash"
    return f"Looks like an agentl bug: run `{target}` to file it (silence with {QUIET_ENV}=1)"


def save_crash(argv: list[str], trace: str) -> Path | None:
    """Keep the traceback of a CLI crash for `agentl report --crash`. Best effort."""
    path = Path(CRASH_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("$ agentl " + " ".join(argv) + "\n" + trace, encoding="utf-8")
    except OSError:
        return None
    return path


# ---------------------------------------------------------------- build
def environment() -> dict:
    from . import __version__

    return {
        "agentl": __version__,
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "implementation": sys.implementation.name,
    }


def _step_kind(pipeline_text: str | None, step_id: str | None) -> tuple[str | None, str | None]:
    """(kind, connector) of the failing step: kind is use, fn, agent or sql."""
    if not pipeline_text or not step_id:
        return None, None
    try:
        import yaml

        from .mcp_server import _Placeholders
        from .spec import _Loader, parse

        spec = parse(yaml.load(pipeline_text, Loader=_Loader), env=_Placeholders())
        step = spec.step(step_id)
    except Exception:
        return None, None
    for kind in ("use", "fn", "agent", "sql"):
        value = getattr(step, kind, None)
        if value:
            return kind, value if isinstance(value, str) else None
    return None, None


def _repro(step: str | None, kind: str | None, connector: str | None, error: str | None) -> str:
    if not step:
        return "Run the same command again with the smallest input that still fails."
    lines = [
        f"1. Keep only the steps up to `{step}` and the inputs it reads.",
        "2. Replace the source with 2 or 3 inline or CSV records that still trigger the error "
        "(no real data).",
    ]
    if kind == "use" and connector:
        lines.append(f"3. Point `{connector}` at a local file or a stub instead of a real service.")
    if kind == "agent":
        lines.append("3. Use `provider: echo` so the repro needs no API key and no network.")
    lines.append("Then run `agentl run repro.yaml` and check it still fails with the same error.")
    if error:
        lines.append(f"Expected error: `{error.splitlines()[0][:200]}`")
    return "\n".join(lines)


def build(
    store: Any | None,
    run_id: str | None,
    pipeline_path: str | None = None,
    crash: str | None = None,
) -> dict:
    """The report as data plus `markdown` and `url`. `crash` is a saved CLI traceback."""
    pipeline_text = Path(pipeline_path).read_text(encoding="utf-8") if pipeline_path else None
    diag: dict | None = None
    doctor = None
    if run_id and store is not None:
        diag = assist.diagnose(store, run_id)
        doctor = assist.explain(diag)
    step = diag["failed_step"] if diag else None
    failure = (diag or {}).get("failure") or {}
    error = failure.get("error") or (diag or {}).get("error")
    trace = failure.get("traceback") or crash
    kind, connector = _step_kind(pipeline_text, step)
    where = f"step `{step}`" + (f" ({connector})" if connector else "") if step else "the CLI"
    last = (crash or "").strip().splitlines()[-1:]
    summary = (error or (last[0] if last else "unexpected error"))[:120]
    report = {
        "title": f"[bug] {summary}" if error or crash else "[bug] ",
        "environment": environment(),
        "run_id": run_id,
        "pipeline": diag["pipeline"] if diag else None,
        "failed_step": step,
        "step_kind": kind,
        "connector": connector,
        "error": _mask(error),
        "traceback": _mask(trace),
        "doctor": _mask(doctor),
        "pipeline_yaml": mask_yaml(pipeline_text) if pipeline_text else None,
        "repro": _repro(step, kind, connector, error),
        "likely_bug": bool(crash) or likely_bug(error, kind),
        "where": where,
        "notice": NOTICE,
    }
    report["markdown"] = markdown(report)
    report["url"] = issue_url(report)
    report["url_too_long"] = report["url"] is None
    return report


# ---------------------------------------------------------------- render
def _fields(r: dict) -> dict[str, str]:
    """The bug-report form fields (ids from .github/ISSUE_TEMPLATE/bug_report.yml)."""
    env = r["environment"]
    what = [f"agentl failed in {r['where']}" + (f" (run `{r['run_id']}`)" if r["run_id"] else "") + "."]
    if r["error"]:
        what.append(f"Error: `{r['error'].splitlines()[0]}`")
    what += ["", "Expected: <what you expected instead>"]
    if r["doctor"]:
        what += ["", "agentl doctor:", "```", r["doctor"], "```"]
    what += ["", "Minimal repro suggestion:", r["repro"]]
    repro = r["pipeline_yaml"] or "# paste the smallest pipeline that fails (agentl report --pipeline p.yaml)"
    return {
        "what": "\n".join(what),
        "repro": repro,
        "logs": r["traceback"] or r["error"] or "",
        "version": env["agentl"],
        "env": f"Python {env['python']} ({env['implementation']}), {env['os']}",
    }


def markdown(r: dict) -> str:
    f = _fields(r)
    return "\n".join(
        [
            f"<!-- {r['title']} -->",
            f"<!-- {NOTICE} -->",
            "### What happened?",
            "",
            f["what"],
            "",
            "### Minimal pipeline to reproduce",
            "",
            "```yaml",
            f["repro"].rstrip("\n"),
            "```",
            "",
            "### Output or traceback",
            "",
            "```shell",
            f["logs"].rstrip("\n"),
            "```",
            "",
            "### agentl version",
            "",
            f["version"],
            "",
            "### Python version and OS",
            "",
            f["env"],
            "",
        ]
    )


def issue_url(r: dict) -> str | None:
    """A prefilled new-issue URL (issue forms take field ids as query parameters), or None when it
    would be longer than URL_LIMIT."""
    query = {"template": TEMPLATE, "title": r["title"], **_fields(r)}
    url = f"{REPO}/issues/new?{urlencode(query)}"
    return url if len(url) <= URL_LIMIT else None


def text(r: dict, saved: str | None = None) -> str:
    lines = [r["markdown"], "---"]
    if r["url"]:
        lines.append("Open this URL to file it (the form is prefilled; review before submitting):")
        lines.append(r["url"])
    else:
        where = saved or "the markdown above"
        lines.append(
            f"The report is too long for a URL. Open {REPO}/issues/new?template={TEMPLATE} and paste "
            f"{where} into the form."
        )
    lines.append(f"Want to fix it yourself, or with your AI agent? See {CONTRIBUTING_AI}")
    lines.append(NOTICE)
    return "\n".join(lines)
