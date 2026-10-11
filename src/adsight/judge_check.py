"""How far to trust the judge: run it on hand-labelled root-cause answers and compare.

Each label (benchmark/judge/labels.jsonl) holds a question, its reference cause, an agent's
answer and a person's verdict on both judge fields. The judge runs through the normal
provider layer (cache, limits, budget), so a repeat check costs nothing.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from adsight.benchmark.answer import AgentAnswer
from adsight.judge import JudgeJSON, judge_root_cause
from adsight.llm.base import InvalidLLMOutput, LLMProvider

FIELDS = ("identifies_true_cause", "invents_unsupported_cause")


class Label(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    source: str = ""
    question: str
    reference: str
    answer_kind: Literal["answer", "clarify", "cannot_answer"]
    answer_summary: str
    identifies_true_cause: bool
    invents_unsupported_cause: bool
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.identifies_true_cause and not self.invents_unsupported_cause


def load_labels(path: Path) -> list[Label]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Label.model_validate(json.loads(line)) for line in lines if line.strip()]


def _verdict(identifies: bool, invents: bool) -> dict[str, bool]:
    return {
        "identifies_true_cause": identifies,
        "invents_unsupported_cause": invents,
        "passed": identifies and not invents,
    }


async def _judge(provider: LLMProvider, model_spec: str, label: Label) -> JudgeJSON | None:
    answer = AgentAnswer(kind=label.answer_kind, summary=label.answer_summary)
    try:
        return await judge_root_cause(provider, model_spec, label.question, answer, label.reference)
    except InvalidLLMOutput:
        return None


async def judge_check(
    labels: list[Label], provider: LLMProvider, model_spec: str
) -> dict[str, Any]:
    """Agreement of the judge with the labels: on the final verdict, on each field, a
    confusion table of final verdicts, and every disagreement. All calls go out at once
    (one batch wave when batching); a judge that answers invalidly twice disagrees."""
    verdicts = await asyncio.gather(*(_judge(provider, model_spec, lb) for lb in labels))
    field_agree = dict.fromkeys(FIELDS, 0)
    confusion = {"both_pass": 0, "both_fail": 0, "judge_only_pass": 0, "label_only_pass": 0}
    final_agree, invalid = 0, 0
    disagreements = []
    for label, judge in zip(labels, verdicts, strict=True):
        if judge is None:
            invalid += 1
            disagreements.append(
                {
                    "id": label.id,
                    "label": _verdict(label.identifies_true_cause, label.invents_unsupported_cause),
                    "judge": None,
                    "reasoning": "invalid output twice",
                }
            )
            continue
        for name in FIELDS:
            field_agree[name] += int(getattr(judge, name) == getattr(label, name))
        key = {
            (True, True): "both_pass",
            (False, False): "both_fail",
            (False, True): "judge_only_pass",
            (True, False): "label_only_pass",
        }[(label.passed, judge.passed)]
        confusion[key] += 1
        same = label.passed == judge.passed and all(
            getattr(judge, name) == getattr(label, name) for name in FIELDS
        )
        final_agree += int(label.passed == judge.passed)
        if not same:
            disagreements.append(
                {
                    "id": label.id,
                    "label": _verdict(label.identifies_true_cause, label.invents_unsupported_cause),
                    "judge": _verdict(judge.identifies_true_cause, judge.invents_unsupported_cause),
                    "reasoning": judge.reasoning,
                }
            )
    return {
        "model": model_spec,
        "cases": len(labels),
        "final_agreement": final_agree,
        "field_agreement": field_agree,
        "confusion": confusion,
        "invalid": invalid,
        "disagreements": disagreements,
    }
