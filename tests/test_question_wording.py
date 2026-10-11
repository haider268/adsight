import dataclasses
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from adsight.agent.context import schema_card
from adsight.benchmark.questions import RefuseScoring, RootCauseScoring, load_questions
from adsight.data.build import open_readonly
from adsight.generate.context import build_context
from adsight.generate.templates import (
    PROBES,
    Env,
    Probe,
    available_probes,
    build_env,
    has_word,
    probe_answerable,
    tokens,
)
from adsight.semantic.model import load_model

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
DEMO = REPO_ROOT / "benchmark" / "generated" / "adsight-demo" / "questions.jsonl"
FLAT = REPO_ROOT / "benchmark" / "generated" / "adsight-flat-demo" / "questions.jsonl"
GOLD = REPO_ROOT / "benchmark" / "questions.jsonl"
FLAT_SENTENCE = " Treat a change of less than 5% between the first and last value as roughly flat."
FOLLOW_UP_ANGLES = ("RC_DECOY", "RC_TRACK_DECOY", "RC_TRACK_CHILD")


@pytest.fixture(scope="module")
def demo_con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = open_readonly(bench_data_dir / "adsight.duckdb")
    yield con
    con.close()


@pytest.fixture(scope="module")
def env(demo_con: duckdb.DuckDBPyConnection) -> Env:
    model = load_model(DEMO_YAML)
    return build_env(model, demo_con, build_context(model, demo_con))


def probe(concept: str) -> Probe:
    return next(p for p in PROBES if p.concept == concept)


def test_every_trend_question_states_the_flat_band() -> None:
    for path in (DEMO, FLAT, GOLD):
        trends = [q for q in load_questions(path) if q.category == "trend"]
        assert trends and all(q.question.endswith(FLAT_SENTENCE) for q in trends), path


def test_decoy_and_child_questions_ask_what_it_was() -> None:
    questions = [q for q in load_questions(DEMO) if q.template in FOLLOW_UP_ANGLES]
    assert {q.template for q in questions} == set(FOLLOW_UP_ANGLES)
    assert all(q.question.endswith("? If not, what was?") for q in questions)
    others = [q for q in load_questions(DEMO) if q.category == "root_cause"]
    assert not any(
        "If not, what was?" in q.question for q in others if q.template not in FOLLOW_UP_ANGLES
    )


def test_text_attribute_values_rule_probes_out(env: Env) -> None:
    assert "broad_45_65" in env.text_values and "image" in env.text_values
    concepts = [p.concept for p in available_probes(env)]
    assert "demographics" not in concepts
    assert not any("age group" in q.question for q in load_questions(DEMO))
    bare = dataclasses.replace(env, text_values=["feeds", "reels"])
    assert not probe_answerable(bare, probe("demographics"))
    ages = dataclasses.replace(env, text_values=["broad_45_65"])
    assert probe_answerable(ages, probe("demographics"))
    assert not probe_answerable(ages, probe("geography"))
    cities = dataclasses.replace(env, text_values=["city_leeds"])
    assert probe_answerable(cities, probe("geography"))
    dated = dataclasses.replace(env, text_values=["2026-05", "q2_2026"])
    assert not probe_answerable(dated, probe("demographics"))


def test_probe_words_match_whole_tokens(env: Env) -> None:
    def ruled_out(concept: str, columns: list[str], values: list[str]) -> bool:
        return probe_answerable(
            dataclasses.replace(env, columns=columns, text_values=values), probe(concept)
        )

    for word in ("image", "advantage_plus", "cost"):
        for concept in ("demographics", "device"):
            assert not ruled_out(concept, [word], []), (word, concept)
            assert not ruled_out(concept, [], [word]), (word, concept)
    assert ruled_out("demographics", ["age_group"], [])
    assert ruled_out("demographics", [], ["broad_45_65"])
    assert ruled_out("video", ["video_views"], [])
    assert not ruled_out("video", ["views"], [])
    assert ruled_out("device", ["os"], [])
    assert has_word("device_os", "os") and not has_word("cost", "os")
    assert tokens("Video_Views 7d") == ["video", "views", "7d"]


def test_card_lists_unmapped_columns(demo_con: duckdb.DuckDBPyConnection) -> None:
    model = load_model(DEMO_YAML)
    card = schema_card(model, build_context(model, demo_con), demo_con)
    assert card == schema_card(model, build_context(model, demo_con), demo_con)
    assert len(card) < 12000
    ad_sets = next(line for line in card.splitlines() if line.startswith("ad_sets: "))
    assert ad_sets.endswith("; other columns: daily_budget DECIMAL(12,2)")
    log = next(line for line in card.splitlines() if "[change log]" in line)
    assert "; other columns: event_id INTEGER; entity types: " in log


def test_gold_text_changes() -> None:
    gold = {q.id: q for q in load_questions(GOLD)}
    un04 = gold["UN04"]
    assert un04.question == "What is the gender split of Northside Home Services' leads?"
    assert isinstance(un04.scoring, RefuseScoring)
    assert un04.scoring.reason == "There is no gender breakdown in the data."
    for q in gold.values():
        if q.notes and q.id not in ("LK07", "RC12", "RC15"):
            assert f"({q.notes})" in q.question, q.id
    assert gold["LK07"].question == (
        "What is the current daily budget of the Homeowners 35-65 ad set "
        "(current = at the end of the data)?"
    )
    assert gold["RC12"].question.endswith("rose in the week of 18 May?")
    assert gold["RC15"].question.endswith("cause the lead drop on 8 and 9 June?")
    assert gold["AG01"].question == (
        "Which campaign had the lowest cost per lead over the last 30 days? "
        "(last 30 days = 2026-05-31 to 2026-06-29)"
    )


def test_no_question_hints_at_its_answer(bench_manifest: dict[str, Any]) -> None:
    sets = [load_questions(path) for path in (GOLD, DEMO, FLAT)]
    summaries = [
        q.scoring.reference_summary
        for questions in sets
        for q in questions
        if isinstance(q.scoring, RootCauseScoring) and q.scoring.reference_summary
    ]
    causes = [str(i["cause"]) for i in bench_manifest["incidents"]]
    assert summaries and causes
    for questions in sets:
        for q in questions:
            text = q.question.casefold()
            assert not re.search(r"\bdecoy", text), q.id
            for hint in [*summaries, *causes]:
                assert hint.casefold() not in text, (q.id, hint)
