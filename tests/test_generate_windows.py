import re
from datetime import date
from pathlib import Path

from adsight.benchmark.questions import load_questions
from adsight.generate.templates import span_text

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_SET = REPO_ROOT / "benchmark" / "generated" / "adsight-demo"
LOGGED_ANGLES = ("RC_SCOPE", "RC_PARENT", "RC_DECOY", "RC_SPEND")
TWO_WEEKS = re.compile(
    r"in the two weeks from (\d{1,2} \w+ 2026) \(([^)]+)\), compared with the two weeks "
    r"before\?( If not, what was\?)?$"
)
ONE_WEEK = re.compile(
    r"in the week from (\d{1,2} \w+ 2026) \(([^)]+)\), compared with the two weeks "
    r"before\?( If not, what was\?)?$"
)

# The human-reviewed questions, as recorded in M3. Their text must never change, except
# RC11 and RC12, which gained "If not, what was?" and were re-approved on 2026-10-11.
REVIEWED = {
    "RC10": "approved",
    "RC11": "approved",
    "RC12": "approved",
    "RC13": "approved",
    "RC16": "approved",
    "RC17": "rejected",
    "RC18": "approved",
    "RC19": "approved",
}


def test_span_text() -> None:
    assert span_text(date(2026, 3, 16), date(2026, 3, 29)) == "16 to 29 March 2026"
    assert span_text(date(2026, 4, 20), date(2026, 5, 3)) == "20 April to 3 May 2026"
    assert span_text(date(2025, 12, 28), date(2026, 1, 10)) == (
        "28 December 2025 to 10 January 2026"
    )


def test_logged_angles_state_their_windows() -> None:
    questions = {q.id: q for q in load_questions(DEMO_SET / "questions.jsonl")}
    by_angle: dict[str, list[str]] = {}
    for q in questions.values():
        if q.template in LOGGED_ANGLES:
            by_angle.setdefault(q.template, []).append(q.question)
    assert set(by_angle) == set(LOGGED_ANGLES)
    for texts in by_angle.values():
        for text in texts:
            assert TWO_WEEKS.search(text) or ONE_WEEK.search(text), text
    assert questions["RC02"].question == (
        'Why did cost per lead rise for the ad set "Women 30-55 Broad" in the two weeks from '
        "16 March 2026 (16 to 29 March 2026), compared with the two weeks before?"
    )
    assert questions["RC03"].question.endswith(
        "in the week from 18 May 2026 (18 to 24 May 2026), compared with the two weeks before?"
    )
    assert questions["RC04"].question.startswith("Was the budget change from 35.00 to 38.00")
    assert questions["RC09"].question == (
        'Why did spend for the campaign "HVAC Tune-Up" jump in the two weeks from 20 April 2026 '
        "(20 April to 3 May 2026), compared with the two weeks before?"
    )


def test_other_root_cause_wording_and_reviews_unchanged() -> None:
    questions = {q.id: q for q in load_questions(DEMO_SET / "questions.jsonl")}
    for q in questions.values():
        if q.category == "root_cause" and q.template not in LOGGED_ANGLES:
            assert "compared with the two weeks before" not in q.question, q.id
    assert {i: questions[i].review for i in REVIEWED} == REVIEWED
    assert questions["RC17"].question == (
        'Why did leads for the campaign "Membership Promo" drop in the week of 8 June 2026?'
    )
