"""The LLM judge: it decides root-cause questions.

The judge checks that an explanation names the same cause as the reference and invents
none; with a judge on, its verdict is the root-cause pass. The regex rule is still computed
and reported as its own "rule check". `bench judge-check` measures the judge against the
hand labels in benchmark/judge/labels.jsonl.
"""

from typing import Any

from pydantic import BaseModel, ValidationError

from adsight.benchmark.answer import AgentAnswer
from adsight.benchmark.questions import Question, RootCauseScoring
from adsight.llm.base import InvalidLLMOutput, LLMProvider
from adsight.llm.factory import parse_spec
from adsight.llm.schema import portable_schema
from adsight.llm.types import LLMRequest, Msg

JUDGE_SYSTEM = (
    "You grade an ads analyst's explanation of why a metric changed, against the known true "
    "cause (the reference). Write your reasoning first, then decide two things.\n"
    "identifies_true_cause: true when the explanation names the same cause as the reference: "
    "the same kind of event or condition (for example a paused ad, a budget change, an "
    "audience change, broken conversion tracking, creative fatigue, a shorter or longer "
    "month, or ordinary week-to-week variation with no logged change) on the same entity or "
    "one of its parents or children. A symptom is not a cause: 'conversions dropped' or 'the "
    "conversion rate collapsed' does not identify broken tracking unless tracking or "
    "measurement is named as the explanation. When the reference says nothing changed and "
    "the movement is normal variation, an explanation that says no logged change explains "
    "it and that the movement came from the conversion rate or a short-lived spike while "
    "spend and clicks were steady does identify the cause. When the reference is about month "
    "length, the explanation must point to the number of days or to steady spend per day.\n"
    "invents_unsupported_cause: true only when the explanation asserts, as a cause, a "
    "different event or condition that the reference does not contain (for example blaming "
    "a budget change when the reference says the month was shorter). Describing how the true "
    "cause worked (for example diminishing returns after a budget increase, or a lower "
    "conversion rate), quoting numbers, ruling a cause out, or mentioning a possibility "
    "alongside the true cause is not inventing."
)
JUDGE_USER = (
    "Question: {question}\n\n"
    "Reference (the true cause): {reference}\n\n"
    "Agent's answer (kind {kind}):\n{summary}\n{values}"
)
JUDGE_MAX_TOKENS = 1500
RETRY_SALT = "retry1"


class JudgeJSON(BaseModel):
    reasoning: str
    identifies_true_cause: bool
    invents_unsupported_cause: bool

    @property
    def passed(self) -> bool:
        return self.identifies_true_cause and not self.invents_unsupported_cause


def reference_text(question: Question, manifest: dict[str, Any] | None) -> str | None:
    """A generated question's reference_summary, or the manifest causes of a gold
    question's incidents (as in reference_answer). None when neither is available."""
    scoring = question.scoring
    if not isinstance(scoring, RootCauseScoring):
        return None
    if scoring.reference_summary is not None:
        return scoring.reference_summary
    if manifest is None:
        return None
    causes = {str(i["incident_id"]): str(i["cause"]) for i in manifest.get("incidents", [])}
    if any(iid not in causes for iid in scoring.incident_ids):
        return None
    return " ".join(causes[iid] for iid in scoring.incident_ids)


def judge_request(
    model_spec: str, question: str, answer: AgentAnswer, reference: str
) -> LLMRequest:
    spec = parse_spec(model_spec)
    values = "; ".join(f"{v.label} = {float(v.value)!r}" for v in answer.values)
    text = JUDGE_USER.format(
        question=question,
        reference=reference,
        kind=answer.kind,
        summary=answer.summary,
        values=f"Values: {values}" if values else "",
    )
    return LLMRequest(
        provider=spec.provider,
        model=spec.model,
        system=JUDGE_SYSTEM,
        messages=[Msg(role="user", text=text)],
        schema=portable_schema(JudgeJSON),
        schema_name="JudgeJSON",
        max_tokens=JUDGE_MAX_TOKENS,
    )


async def judge_root_cause(
    provider: LLMProvider,
    model_spec: str,
    question: str,
    answer: AgentAnswer,
    reference: str,
    salt: str = "",
) -> JudgeJSON:
    """One judge call; invalid output is retried once with a new cache key, then raises
    InvalidLLMOutput."""
    req = judge_request(model_spec, question, answer, reference)
    retry = f"{salt}:{RETRY_SALT}" if salt else RETRY_SALT
    raw = ""
    for attempt_salt in (salt, retry):
        try:
            resp = await provider.complete(req, attempt_salt)
        except InvalidLLMOutput as exc:
            raw = exc.raw_text
            continue
        try:
            return JudgeJSON.model_validate(resp.data)
        except ValidationError:
            raw = resp.raw_text
            continue
    raise InvalidLLMOutput(raw, "judge output invalid twice")


def combine(rule_passed: bool, judge: JudgeJSON | None) -> bool:
    """The final pass of a root-cause question: the judge's verdict (the rule when the judge
    is off or gave no verdict)."""
    return rule_passed if judge is None else judge.passed
