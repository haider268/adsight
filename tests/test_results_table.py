import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adsight.cli import app
from adsight.results_table import fabrication, load_jobs, results_markdown, write_results

SVG = "{http://www.w3.org/2000/svg}"
runner = CliRunner()


def answer(qid: str, category: str, passed: bool, kind: str = "answer", values: int = 1) -> Any:
    return {
        "question_id": qid,
        "category": category,
        "passed": passed,
        "rule_passed": passed,
        "judge_identifies": None,
        "judge_invents": None,
        "kind": kind,
        "values": values,
    }


def report(
    arch: str,
    lookup: tuple[int, int],
    rc: tuple[int, int, int],
    unanswerable: list[tuple[str, int]],
    calls: float,
    cost: float,
    questions_dir: str = "benchmark/generated/adsight-demo",
    sample: int | None = None,
    salt: str = "",
    with_answers: bool = True,
) -> dict[str, Any]:
    un_passed = sum(1 for kind, values in unanswerable if kind == "cannot_answer" and not values)
    per_category: dict[str, Any] = {
        "lookup": {"passed": lookup[0], "total": lookup[1]},
        "root_cause": {"passed": rc[0], "total": rc[2], "rule_passed": rc[1]},
        "unanswerable": {"passed": un_passed, "total": len(unanswerable)},
    }
    answers = [
        answer(f"UN{i:02d}", "unanswerable", kind == "cannot_answer" and not values, kind, values)
        for i, (kind, values) in enumerate(unanswerable, 1)
    ]
    data: dict[str, Any] = {
        "arch": arch,
        "model_spec": "fixture:scripted",
        "judge": "fixture:scripted",
        "questions_dir": questions_dir,
        "sample": sample,
        "salt": salt,
        "per_category": per_category,
        "mean_llm_calls": calls,
        "mean_tokens": 1000.0 * calls,
        "total_cost_usd": cost,
    }
    if with_answers:
        data["answers"] = answers
    return data


REFUSE = ("cannot_answer", 0)
FABRICATE = ("answer", 2)
NO_VALUES = ("answer", 0)


@pytest.fixture
def results(tmp_path: Path) -> Path:
    folder = tmp_path / "results"
    jobs = {
        "demo-b0-v9": report("b0", (3, 4), (1, 2, 4), [REFUSE, FABRICATE], 2.0, 0.10),
        "demo-b2-v9": report("b2", (4, 4), (3, 1, 4), [REFUSE, NO_VALUES], 2.5, 0.20),
        "demo-b4-v9": report("b4", (4, 4), (4, 3, 4), [FABRICATE, FABRICATE], 4.25, 0.40),
        "gold-b0-v9": report("b0", (2, 4), (0, 0, 4), [REFUSE], 2.0, 0.05, "benchmark"),
        "gold-b4-v9": report("b4", (3, 4), (2, 1, 4), [REFUSE], 4.0, 0.06, "benchmark"),
        "var-b4-1-v9": report("b4", (2, 2), (1, 1, 2), [REFUSE], 4.0, 0.01, sample=5, salt="r1"),
        "var-b4-2-v9": report("b4", (1, 2), (1, 1, 2), [FABRICATE], 4.0, 0.01, sample=5, salt="r2"),
    }
    for job, data in jobs.items():
        (folder / job).mkdir(parents=True)
        (folder / job / "report.json").write_text(json.dumps(data), encoding="utf-8")
    (folder / "summary.json").write_text(json.dumps({"plan": "fixture-v9"}), encoding="utf-8")
    return folder


def test_job_kinds(results: Path) -> None:
    kinds = {j.id: j.kind for j in load_jobs(results)}
    assert kinds["demo-b2-v9"] == "demo" and kinds["gold-b4-v9"] == "gold"
    assert kinds["var-b4-1-v9"] == "variance"


def test_table_cells(results: Path) -> None:
    md = results_markdown(load_jobs(results), "t")
    assert "| category | B0 | B2 | B4 |" in md
    assert "| lookup | 3/4 | 4/4 | 4/4 |" in md
    assert "| root_cause | 1/4 | 3/4 | 4/4 |" in md
    assert "| root_cause (rule check) | 2/4 | 1/4 | 3/4 |" in md
    assert "| overall | 5/10 | 8/10 | 8/10 |" in md
    assert "| B4 | 4.25 | 4250.0 | $0.4000 |" in md
    assert "Total cost of every job in this folder: $0.8300." in md
    assert "| category | B0 | B4 |" in md and "| overall | 3/9 | 6/9 |" in md
    assert "| var-b4-1-v9 | B4 | 4/5 | 80.0% |" in md
    assert "| var-b4-2-v9 | B4 | 2/5 | 40.0% |" in md
    assert "Difference between the two runs: 40.0 percentage points." in md
    assert "Questions whose outcome differs between the runs: 1." in md


def test_fabrication_rate(results: Path) -> None:
    jobs = {j.id: j for j in load_jobs(results)}
    assert fabrication(jobs["demo-b0-v9"]) == (1, 2)
    assert fabrication(jobs["demo-b2-v9"]) == (0, 2)  # an answer with no values is not made up
    assert fabrication(jobs["demo-b4-v9"]) == (2, 2)
    md = results_markdown(list(jobs.values()), "t")
    assert "| B0 | 1/2 | 50.0% |" in md and "| B4 | 2/2 | 100.0% |" in md


def test_reports_without_answers_say_so(tmp_path: Path) -> None:
    folder = tmp_path / "old"
    (folder / "demo-b0").mkdir(parents=True)
    data = report("b0", (1, 1), (0, 0, 1), [REFUSE], 2.0, 0.1, with_answers=False)
    (folder / "demo-b0" / "report.json").write_text(json.dumps(data), encoding="utf-8")
    assert "| B0 | n/a (no per-question answers) | n/a |" in results_markdown(
        load_jobs(folder), "t"
    )


def test_svgs_parse_with_one_mark_per_architecture(results: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    written = write_results(results, out)
    assert sorted(p.name for p in written) == [
        "accuracy_by_category.svg",
        "cost_vs_accuracy.svg",
        "results.md",
    ]
    assert (
        (out / "results.md")
        .read_text(encoding="utf-8")
        .startswith("# Benchmark results: fixture-v9")
    )
    bars = ET.parse(out / "accuracy_by_category.svg").getroot()
    assert bars.get("width") == "760"
    rects = [r for r in bars.iter(f"{SVG}rect") if r.get("class") == "bar"]
    overall = {
        r.get("data-arch"): r.get("data-value")
        for r in rects
        if r.get("data-category") == "overall"
    }
    assert overall == {"b0": "0.5000", "b2": "0.8000", "b4": "0.8000"}
    lookup = {
        r.get("data-arch"): r.get("data-value") for r in rects if r.get("data-category") == "lookup"
    }
    assert lookup == {"b0": "0.7500", "b2": "1.0000", "b4": "1.0000"}
    assert len(rects) == 3 * 4  # three architectures, three categories plus overall
    points = ET.parse(out / "cost_vs_accuracy.svg").getroot()
    circles = {
        c.get("data-arch"): (c.get("data-calls"), c.get("data-accuracy"))
        for c in points.iter(f"{SVG}circle")
    }
    assert circles == {
        "b0": ("2.0", "0.5000"),
        "b2": ("2.5", "0.8000"),
        "b4": ("4.25", "0.8000"),
    }
    texts = [t.text for t in points.iter(f"{SVG}text")]
    assert "B4 80.0%" in texts


def test_cli_table(results: Path, tmp_path: Path) -> None:
    out = tmp_path / "docs"
    ok = runner.invoke(app, ["bench", "table", "--results", str(results), "--out", str(out)])
    assert ok.exit_code == 0, ok.output
    assert (out / "results.md").is_file()
    empty = tmp_path / "empty"
    empty.mkdir()
    bad = runner.invoke(app, ["bench", "table", "--results", str(empty), "--out", str(out)])
    assert bad.exit_code == 1
