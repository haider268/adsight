import re
from pathlib import Path

from adsight.agent.prompts import ANSWER_FIELDS
from adsight.benchmark.answer import AgentAnswer
from adsight.benchmark.questions import Question, RootCauseScoring, load_questions
from adsight.benchmark.score import normalise_entity, score

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO = REPO_ROOT / "benchmark" / "generated" / "adsight-demo" / "questions.jsonl"
GOLD = REPO_ROOT / "benchmark" / "questions.jsonl"


def by_id(path: Path) -> dict[str, Question]:
    return {q.id: q for q in load_questions(path)}


def entities(*names: str) -> AgentAnswer:
    return AgentAnswer(kind="answer", summary="", entities=list(names))


def test_normalise_entity() -> None:
    for month in ("2026-05", "May 2026", "may 2026", "2026-05-01", " 'May  2026' ", "MAY 2026"):
        assert normalise_entity(month) == "2026-05", month
    assert normalise_entity("Feb 2026") == "2026-02"
    assert normalise_entity('"Advantage_Plus"') == "advantage plus"
    assert normalise_entity("  Women   30-55 Broad ") == "women 30-55 broad"
    assert normalise_entity("Mayday 2026") == "mayday 2026"


def test_month_forms_match() -> None:
    ag03 = by_id(DEMO)["AG03"]  # expected 2026-02
    assert ag03.expected is not None and ag03.expected.entity_id == "2026-02"
    assert score(ag03, entities("February 2026")).passed
    assert score(ag03, entities("2026-02-01")).passed
    assert not score(ag03, entities("March 2026")).passed


def test_scope_entity_is_skipped_but_a_wrong_winner_fails() -> None:
    ag10 = by_id(GOLD)["AG10"]  # placement in the Lumen Med Spa account
    assert ag10.expected is not None and ag10.expected.entity_id == "advantage_plus"
    assert score(ag10, entities("Lumen Med Spa", "Advantage Plus")).passed
    assert score(ag10, entities("advantage_plus")).passed
    wrong = score(ag10, entities("Lumen Med Spa", "feeds", "advantage_plus"))
    assert not wrong.passed and "'feeds'" in wrong.reason
    assert not score(ag10, entities("Lumen Med Spa")).passed


def test_quoted_names_are_skipped() -> None:
    ag01 = by_id(DEMO)["AG01"]  # which ad in the ad set "Lookalike Customers"
    assert score(ag01, entities('"Lookalike Customers"', "AD043")).passed
    assert score(ag01, entities("Lookalike Customers", "Lookalike Customers | Video")).passed
    assert not score(ag01, entities("Lookalike Customers", "AD042")).passed


def test_answer_fields_ask_for_the_answer_entity_and_unrounded_values() -> None:
    assert (
        "entities lists only the answer itself (the winning item first), never the entity the "
        "question is scoped to." in ANSWER_FIELDS
    )
    assert "values are unrounded, with at least 4 significant digits." in ANSWER_FIELDS


def rc(template: str) -> Question:
    return next(q for q in load_questions(DEMO) if q.template == template)


def required(q: Question) -> list[str]:
    assert isinstance(q.scoring, RootCauseScoring)
    return q.scoring.required


def test_parent_question_passes_on_the_changed_values() -> None:
    rc06 = by_id(DEMO)["RC06"]
    assert rc06.template == "RC_PARENT"
    assert required(rc06)[-1] == r"S13|Homeowners\ 35\-65|55\.00|110\.00|\b55\b|\b110\b"
    summary = "The ad set's daily budget was increased from 55 to 110 on 20 April."
    assert score(rc06, AgentAnswer(kind="answer", summary=summary)).passed
    assert not re.search(required(rc06)[-1], "a budget of 550 was raised to 1100")


def test_tracking_child_needs_tracking_only_and_noise_accepts_efficiency() -> None:
    child = rc("RC_TRACK_CHILD")
    assert required(child) == ["tracking|pixel|measurement|conversion api|capi"]
    noise = rc("RC_NOISE")
    summary = "No logged change; the jump came from better conversion efficiency."
    assert score(noise, AgentAnswer(kind="answer", summary=summary)).passed
