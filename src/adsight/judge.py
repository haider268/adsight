"""The LLM judge: a second layer on root-cause scoring.

The rule layer checks that the explanation mentions the right things; the judge checks
that it names the same cause as the reference and invents none. A root-cause question
passes only when both layers agree.
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
    "You grade an analytics agent's explanation against the known true cause. Write your "
    "reasoning first. identifies_true_cause is true only if the explanation names the same "
    "cause as the reference (the same entity and the same kind of change or problem). "
    "invents_unsupported_cause is true if it asserts any cause that is not in the reference "
    "and is not supported by data it quotes. Ruling a cause out is not inventing it."
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
    """The final pass of a root-cause question: rule AND judge (the rule alone when off)."""
    return rule_passed if judge is None else rule_passed and judge.passed
