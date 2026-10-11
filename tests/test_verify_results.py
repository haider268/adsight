import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import adsight.verify_results as verify_module
from adsight.cli import app
from adsight.verify_results import MAX_TRIES, NoNetworkClient, OfflineOnly, verify_results

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS = REPO_ROOT / "benchmark" / "results" / "haiku-demo-v3"
DEMO_YAML = REPO_ROOT / "semantic" / "demo.yaml"

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path, bench_data_dir: Path, bench_manifest: dict[str, Any]) -> Path:
    """The seed-7 database and manifest, copied so no other open connection interferes."""
    folder = tmp_path / "data"
    folder.mkdir()
    shutil.copyfile(bench_data_dir / "adsight.duckdb", folder / "adsight.duckdb")
    (folder / "manifest.json").write_text(json.dumps(bench_manifest), encoding="utf-8")
    return folder


def published(*jobs: str, into: Path) -> Path:
    """Copies of committed job folders (the originals are never touched)."""
    for job in jobs:
        shutil.copytree(RESULTS / job, into / job)
    return into


@pytest.fixture(autouse=True)
def at_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)  # reports hold questions_dir relative to the repo
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_published_demo_b4_and_gold_b0_replay_exactly(data_dir: Path, tmp_path: Path) -> None:
    # Their reports carry no render field, so they replay as render v3, with up to 3 tries
    # for DuckDB's occasional reordering of tied rows.
    folder = published("demo-b4-v3", "gold-b0-v3", into=tmp_path / "res")
    assert all("render" not in json.loads(p.read_text()) for p in folder.glob("*/report.json"))
    checks = {c.job: c for c in verify_results(folder, data_dir, DEMO_YAML)}
    assert all(c.match and c.tries <= MAX_TRIES for c in checks.values()), checks
    assert checks["demo-b4-v3"].replayed == 0.9293
    assert checks["gold-b0-v3"].replayed == 0.8


def test_cli_match_then_tampered(data_dir: Path, tmp_path: Path) -> None:
    from rich.console import Console

    import adsight.cli

    adsight.cli.console = Console(width=200)
    folder = published("var-b4-1-v3", into=tmp_path / "res")
    args = ["bench", "verify-results", "--results", str(folder), "--data-dir", str(data_dir)]
    ok = runner.invoke(app, args)
    assert ok.exit_code == 0, ok.output
    assert "var-b4-1-v3" in ok.output and "match" in ok.output

    report_path = folder / "var-b4-1-v3" / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["per_category"]["lookup"]["passed"] -= 1
    report_path.write_text(json.dumps(report), encoding="utf-8")
    bad = runner.invoke(app, args)
    assert bad.exit_code == 1, bad.output
    assert "mismatch after 3 tries" in bad.output and "category counts differ" in bad.output

    empty = tmp_path / "empty"
    empty.mkdir()
    none = runner.invoke(app, ["bench", "verify-results", "--results", str(empty)])
    assert none.exit_code in (1, 2)


def test_replay_client_refuses_every_use() -> None:
    with pytest.raises(OfflineOnly):
        _ = NoNetworkClient().messages


def test_a_chance_mismatch_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = tmp_path / "job"
    job.mkdir()
    report = {"accuracy": 0.5, "per_category": {"lookup": {"passed": 1, "total": 2}}}
    (job / "report.json").write_text(json.dumps(report), encoding="utf-8")
    off = {"accuracy": 0.0, "per_category": {"lookup": {"passed": 0, "total": 2}}}
    replies = [(off, 1), (report, 0)]
    monkeypatch.setattr(verify_module, "replay_job", lambda *a: replies.pop(0))
    check = verify_module.check_job(job, tmp_path, DEMO_YAML)
    assert check.match and check.tries == 2 and check.outcome == "match on retry 1"
    monkeypatch.setattr(verify_module, "replay_job", lambda *a: (off, 1))
    check = verify_module.check_job(job, tmp_path, DEMO_YAML)
    assert not check.match and check.tries == 3
    assert check.outcome.startswith("mismatch after 3 tries (replay exit 1; accuracy differs")
