"""What the agent cache key depends on beyond the step itself (#338)."""

import yaml

from agentl import llm, runner
from agentl.spec import parse

PIPELINE = """
name: keys
llm:
  default: {provider: echo, model: m}
steps:
  - id: classify
    agent: Classify the ticket.
    output: {category: str}
"""


def spec_and_step(**extra):
    raw = yaml.safe_load(PIPELINE)
    raw["steps"][0].update(extra)
    spec = parse(raw)
    return spec, spec.steps[0]


def test_prompt_version_is_part_of_the_key(monkeypatch):
    spec, step = spec_and_step()
    rec = {"text": "printer is on fire"}
    before = runner.agent_cache_key(spec, step, rec)
    monkeypatch.setattr(llm, "PROMPT_VERSION", "changed")
    assert runner.agent_cache_key(spec, step, rec) != before
    # the review queue key ignores it, so reviews survive a prompt rewording
    review_key = runner.agent_cache_key(spec, step, rec, prompts=False)
    monkeypatch.setattr(llm, "PROMPT_VERSION", "again")
    assert runner.agent_cache_key(spec, step, rec, prompts=False) == review_key


def test_prompt_version_follows_the_built_in_prompts():
    import hashlib

    expected = f"{llm.FRAMING_REVISION}|{llm.SYSTEM}|{llm.TOOLS_SYSTEM}"
    assert hashlib.sha256(expected.encode()).hexdigest()[:12] == llm.PROMPT_VERSION


def test_cache_version_changes_the_key():
    rec = {"text": "x"}
    keys = {
        runner.agent_cache_key(*spec_and_step(), rec),
        runner.agent_cache_key(*spec_and_step(cache_version=1), rec),
        runner.agent_cache_key(*spec_and_step(cache_version=2), rec),
        runner.agent_cache_key(*spec_and_step(cache_version="2"), rec),
    }
    assert len(keys) == 4
    assert runner.agent_cache_key(*spec_and_step(cache_version=1), rec) == runner.agent_cache_key(
        *spec_and_step(cache_version=1), rec
    )
