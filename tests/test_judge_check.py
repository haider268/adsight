import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adsight.cli import app
from adsight.judge_check import judge_check, load_labels
from adsight.llm.base import InvalidLLMOutput
from adsight.llm.providers.fixture import ScriptedProvider
from adsight.llm.types import LLMRequest

REPO_ROOT = Path(__file__).resolve().parents[1]
LABELS = REPO_ROOT / "benchmark" / "judge" / "labels.jsonl"
SPEC = "fixture:scripted"

runner = CliRunner()


def test_labels_file_holds_thirty_cases() -> None:
    labels = load_labels(LABELS)
    assert len(labels) == 30
    assert len({lb.id for lb in labels}) == 30


def echo_labels(flip: set[str], invalid: frozenset[str] = frozenset()) -> Any:
    """A judge that repeats each label's verdict, flipping identifies_true_cause for
    `flip` and answering invalidly for `invalid`. (J03 and J05 are the same case.)"""
    labels = load_labels(LABELS)

    def script(req: LLMRequest) -> dict[str, Any]:
        text = req.messages[-1].text
        label = next(lb for lb in labels if lb.answer_summary in text and lb.question in text)
        if label.id in invalid:
            raise InvalidLLMOutput("not json")
        identifies = label.identifies_true_cause != (label.id in flip)
        return {
            "reasoning": f"judged {label.id}",
            "identifies_true_cause": identifies,
            "invents_unsupported_cause": label.invents_unsupported_cause,
        }

    return script


def test_agreement_confusion_and_disagreements() -> None:
    labels = load_labels(LABELS)
    passing = next(lb for lb in labels if lb.passed)
    failing = next(lb for lb in labels if not lb.identifies_true_cause and not lb.passed)
    assert not failing.invents_unsupported_cause  # flipping it makes the judge pass it
    provider = ScriptedProvider(echo_labels({passing.id, failing.id}))
    result = asyncio.run(judge_check(labels, provider, SPEC))
    assert result["cases"] == 30
    assert result["final_agreement"] == 28
    assert result["field_agreement"] == {
        "identifies_true_cause": 28,
        "invents_unsupported_cause": 30,
    }
    c = result["confusion"]
    assert sum(c.values()) == 30
    assert c["label_only_pass"] == 1 and c["judge_only_pass"] == 1
    assert [d["id"] for d in result["disagreements"]] == [
        lb.id for lb in labels if lb.id in {passing.id, failing.id}
    ]
    first = next(d for d in result["disagreements"] if d["id"] == passing.id)
    assert first["label"]["passed"] is True and first["judge"]["passed"] is False
    assert first["reasoning"] == f"judged {passing.id}"


def test_perfect_judge_agrees_on_everything() -> None:
    labels = load_labels(LABELS)
    result = asyncio.run(judge_check(labels, ScriptedProvider(echo_labels(set())), SPEC))
    assert result["final_agreement"] == 30 and result["disagreements"] == []
    assert result["confusion"]["judge_only_pass"] == 0
    assert result["confusion"]["label_only_pass"] == 0


def test_invalid_judge_output_counts_as_a_disagreement() -> None:
    labels = load_labels(LABELS)
    bad = labels[0].id
    provider = ScriptedProvider(echo_labels(set(), invalid=frozenset({bad})))
    result = asyncio.run(judge_check(labels, provider, SPEC))
    assert result["invalid"] == 1 and result["final_agreement"] == 29
    assert result["disagreements"][0]["id"] == bad and result["disagreements"][0]["judge"] is None


def test_cli_writes_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rich.console import Console

    import adsight.cli
    from adsight.llm.factory import ProviderPool

    monkeypatch.setattr(adsight.cli, "console", Console(width=200))
    monkeypatch.setenv("ADSIGHT_STATE_DIR", str(tmp_path / "state"))
    flip = {load_labels(LABELS)[0].id}
    monkeypatch.setattr(
        ProviderPool, "get", lambda self, spec, batch=None: ScriptedProvider(echo_labels(flip))
    )
    result = runner.invoke(app, ["bench", "judge-check", "--labels", str(LABELS), "--model", SPEC])
    assert result.exit_code == 0, result.output
    assert "final verdict agrees on 29/30" in result.output
    path = tmp_path / "state" / "reports" / "judge-check-fixture-scripted.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["final_agreement"] == 29 and report["model"] == SPEC
    missing = runner.invoke(app, ["bench", "judge-check", "--labels", str(LABELS)])
    assert missing.exit_code == 2
