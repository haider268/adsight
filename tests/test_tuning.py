import asyncio
import shutil
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from adsight.agent.context import model_tables, schema_card
from adsight.architectures import MAX_LLM_CALLS, max_llm_calls, run_question
from adsight.data.build import open_readonly
from adsight.generate.causes import detect_causes
from adsight.generate.context import build_context
from adsight.generate.lint import lint
from adsight.generate.templates import build_candidate, build_env
from adsight.llm.providers.fixture import ScriptedProvider
from adsight.semantic.model import SemanticModel, dump_model, load_model
from adsight.sql_safety import open_agent_connection
from adsight.tuning import DEFAULT_TUNING, Tuning

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"
LK01 = {"metric": "conversions", "level": "ad", "entity": "AD025", "period": "2026-04"}


@pytest.fixture(scope="module")
def con(bench_data_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    c = open_readonly(bench_data_dir / "adsight.duckdb")
    yield c
    c.close()


def with_tuning(tmp_path: Path, block: str) -> Path:
    """The demo YAML plus a `tuning:` block."""
    path = tmp_path / "tuned.yaml"
    text = DEMO_YAML.read_text(encoding="utf-8").rstrip("\n") + "\ntuning:\n" + block
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_are_the_demo_values() -> None:
    model = load_model(DEMO_YAML)
    assert model.tuning == DEFAULT_TUNING
    assert DEFAULT_TUNING.min_conversions == 20 and DEFAULT_TUNING.material_budget_change == 0.20
    assert DEFAULT_TUNING.trend_flat_band_pct == 5.0 and DEFAULT_TUNING.b1_max_steps == 6
    assert MAX_LLM_CALLS == {"b0": 2, "b1": 6, "b2": 2, "b3": 5, "b4": 7}


def test_linter_threshold_override(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    default = load_model(DEMO_YAML)
    tuned = load_model(with_tuning(tmp_path, "  min_conversions: 100000\n"))
    assert tuned.tuning.min_conversions == 100000
    for model, ok in ((default, True), (tuned, False)):
        ctx = build_context(model, con)
        cand = build_candidate(build_env(model, con, ctx), "L_TOTAL", LK01)
        result = lint(cand, con, model, ctx)
        assert result.ok is ok, result
        if not ok:
            assert result.rule == "R2_VOLUME" and "< 100000" in result.reason


def routine_budget_changes(model: SemanticModel, con: duckdb.DuckDBPyConnection) -> list[str]:
    env = build_env(model, con, build_context(model, con))
    changes = detect_causes(env, con).changes
    return sorted(c.key for c in changes if c.change_type == "budget_change" and not c.material)


def test_detector_threshold_override(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    default = routine_budget_changes(load_model(DEMO_YAML), con)
    assert default == ["S02", "S04", "S08", "S10", "S14", "S16"]  # +10% budget changes
    tuned = load_model(with_tuning(tmp_path, "  material_budget_change: 0.05\n"))
    assert routine_budget_changes(tuned, con) == []


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="min_clickz"):
        load_model(with_tuning(tmp_path, "  min_clickz: 5\n"))
    with pytest.raises(ValidationError):
        Tuning.model_validate({"max_fixes": "many"})


def test_dump_keeps_only_overrides(tmp_path: Path) -> None:
    out = tmp_path / "out.yaml"
    dump_model(load_model(DEMO_YAML), out)
    assert "tuning" not in out.read_text(encoding="utf-8")
    dump_model(load_model(with_tuning(tmp_path, "  max_fixes: 1\n")), out)
    assert "tuning:\n  max_fixes: 1\n" in out.read_text(encoding="utf-8")
    assert load_model(out).tuning.max_fixes == 1


def test_agent_call_limits_follow_the_tuning() -> None:
    tuned = Tuning(b1_max_steps=4, max_fixes=1)
    assert max_llm_calls(tuned) == {"b0": 2, "b1": 4, "b2": 2, "b3": 3, "b4": 5}


def test_b1_step_limit_comes_from_the_dataset(bench_data_dir: Path, tmp_path: Path) -> None:
    tuned = load_model(with_tuning(tmp_path, "  b1_max_steps: 3\n"))
    db = tmp_path / "copy.duckdb"  # the module's read-only connection holds the original
    shutil.copyfile(bench_data_dir / "adsight.duckdb", db)
    agent_con = open_agent_connection(db)
    try:
        card = schema_card(tuned, build_context(tuned, agent_con), agent_con)
        empty = {"kind": "answer", "summary": "", "values": [], "entities": []}
        empty |= {"direction": "none", "assumptions": []}
        step = {"thought": "", "action": "run_sql", "sql": "SELECT 1 AS one", "final": empty}
        provider = ScriptedProvider(lambda req: step)
        tables = set(model_tables(tuned))
        result = asyncio.run(
            run_question(
                "b1",
                "Keep going",
                provider,
                "fixture:scripted",
                card,
                agent_con,
                tables,
                semantic=tuned,
            )
        )
    finally:
        agent_con.close()
    assert provider.calls == 3 and result.answer.summary == "step limit reached"
