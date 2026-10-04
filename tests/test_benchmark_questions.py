from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adsight.benchmark.questions import (
    BENCHMARK_SEED,
    BENCHMARK_TODAY,
    Expected,
    Question,
    load_questions,
    save_questions,
)


def _q(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "LK99",
        "category": "lookup",
        "question": "How much?",
        "scoring": {"type": "numeric", "unit": "usd"},
        "reference_sql": "LK99.sql",
    }
    base.update(overrides)
    return base


def test_constants() -> None:
    assert BENCHMARK_SEED == 7
    assert BENCHMARK_TODAY.isoformat() == "2026-06-30"


def test_valid_question() -> None:
    q = Question.model_validate(_q())
    assert q.scoring.type == "numeric"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": "LK1"}, "bad question id"),
        ({"id": "XX01"}, "bad question id"),
        ({"id": "AG01"}, "does not match category"),
        ({"scoring": {"type": "entity"}}, "lookup questions use 'numeric'"),
        ({"reference_sql": None}, "needs reference_sql"),
        (
            {
                "id": "AM99",
                "category": "ambiguous",
                "scoring": {"type": "clarify"},
                "reference_sql": "AM99.sql",
            },
            "takes no reference_sql",
        ),
        (
            {
                "id": "RC99",
                "category": "root_cause",
                "scoring": {
                    "type": "root_cause",
                    "incident_ids": ["I1"],
                    "required": ["(unclosed"],
                },
                "reference_sql": None,
            },
            "invalid regex",
        ),
        (
            {
                "id": "RC99",
                "category": "root_cause",
                "scoring": {"type": "root_cause", "incident_ids": [], "required": ["x"]},
                "reference_sql": None,
            },
            "needs incident_ids or a reference_summary",
        ),
    ],
)
def test_invalid_questions_raise(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Question.model_validate(_q(**overrides))


def test_save_load_round_trip(tmp_path: Path) -> None:
    questions = [
        Question.model_validate(_q(expected=Expected(values={"total_spend": 1.5}).model_dump())),
        Question.model_validate(
            {
                "id": "RC99",
                "category": "root_cause",
                "question": "Why?",
                "scoring": {
                    "type": "root_cause",
                    "incident_ids": ["I1"],
                    "required": ["AD019|Women 30-55 Broad \\| Video"],
                },
            }
        ),
    ]
    path = tmp_path / "q.jsonl"
    save_questions(questions, path)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert len(text.splitlines()) == 2
    assert load_questions(path) == questions


def test_load_reports_line_number(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text('{"id": "LK01"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"q\.jsonl:1:"):
        load_questions(path)


def test_load_rejects_duplicate_ids(tmp_path: Path) -> None:
    line = Question.model_validate(_q()).model_dump_json()
    path = tmp_path / "q.jsonl"
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r":2: duplicate question id"):
        load_questions(path)


def test_sample_file_loads(questions_path: Path) -> None:
    ids = [q.id for q in load_questions(questions_path)]
    assert len(ids) == len(set(ids))
    assert {"LK01", "AG01", "CP01", "TR01", "RC01", "AM01", "UN01"} <= set(ids)


def test_final_benchmark_shape(questions_path: Path) -> None:
    questions = load_questions(questions_path)
    assert len(questions) == 100
    assert Counter(q.category for q in questions) == {
        "lookup": 15,
        "aggregation": 15,
        "comparison": 15,
        "trend": 15,
        "root_cause": 20,
        "ambiguous": 10,
        "unanswerable": 10,
    }
