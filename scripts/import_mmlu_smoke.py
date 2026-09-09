"""Freeze a small public MMLU split as an offline Agent Eval Benchmark package.

The generated package deliberately contains only 20 questions from the public
``cais/mmlu`` high_school_computer_science/test split. Downloading is a
one-time import step; evaluation itself never needs a network connection.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DATASET = "cais/mmlu"
CONFIG = "high_school_computer_science"
SPLIT = "test"
COUNT = 20
ENDPOINT = "https://datasets-server.huggingface.co/rows"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "benchmarks" / "mmlu-high-school-computer-science-smoke"


def fetch_rows() -> list[dict[str, Any]]:
    cached = os.environ.get("AGENT_EVAL_MMLU_ROWS")
    if cached:
        payload = json.loads(cached)
        rows = payload.get("rows")
        if isinstance(rows, list) and len(rows) == COUNT:
            return rows
        raise RuntimeError("AGENT_EVAL_MMLU_ROWS does not contain the expected MMLU rows")
    query = urllib.parse.urlencode(
        {"dataset": DATASET, "config": CONFIG, "split": SPLIT, "offset": 0, "length": COUNT}
    )
    request = urllib.request.Request(
        ENDPOINT + "?" + query,
        headers={"Accept": "application/json", "User-Agent": "Agent-Eval-MMLU-Importer/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != COUNT:
        raise RuntimeError(f"Expected {COUNT} MMLU rows, received {len(rows) if isinstance(rows, list) else 'invalid data'}")
    return rows


def task_for(index: int, item: dict[str, Any]) -> dict[str, Any]:
    row = item.get("row")
    if not isinstance(row, dict):
        raise RuntimeError(f"Invalid MMLU row at index {index}")
    question = str(row.get("question") or "").strip()
    choices = row.get("choices")
    raw_answer = row.get("answer")
    answer = "ABCD"[raw_answer] if isinstance(raw_answer, int) and 0 <= raw_answer < 4 else raw_answer
    if not question or not isinstance(choices, list) or len(choices) != 4 or answer not in {"A", "B", "C", "D"}:
        raise RuntimeError(f"Invalid MMLU question at index {index}")
    rendered_choices = "\n".join(f"{letter}. {str(choice).strip()}" for letter, choice in zip("ABCD", choices))
    return {
        "id": f"MMLU-HSCS-{index + 1:02d}",
        "suite": "high-school-computer-science",
        "title": f"MMLU 高中计算机科学 #{index + 1}",
        "instruction": (
            "Answer this MMLU multiple-choice question. Do not use tools. Reply with exactly one plain-text "
            "line in the format FINAL: <letter>, where <letter> is A, B, C, or D.\n\n"
            f"Question: {question}\n\n{rendered_choices}"
        ),
        "expected_output": {"answer": f"FINAL: {answer}"},
        "metadata": {"source_row_index": item.get("row_idx", index), "subject": row.get("subject", CONFIG)},
        "tags": ["mmlu", "multiple-choice", "no-tool", "dsh"],
        "case_type": "typical",
        "timeout_seconds": 120,
        "environment": {"type": "fixture", "snapshot": "mmlu-high-school-computer-science-test-v1"},
        "agent_requirements": {"features": ["trace", "usage"], "tools": [], "events": [], "domains": ["general"]},
        "graders": [{"id": "mmlu-answer", "type": "rule", "weight": 1.0, "config": {"regex": rf"(?m)^FINAL:\s*{answer}\s*$", "max_tool_calls": 0}}],
        "hard_failures": ["forbidden_tool", "secret_exposure"],
    }


def write_package(output: Path, rows: list[dict[str, Any]]) -> None:
    if output.exists():
        raise FileExistsError(f"Output already exists and was not overwritten: {output}")
    tasks = [task_for(index, item) for index, item in enumerate(rows)]
    manifest = {
        "benchmark_id": "mmlu-high-school-computer-science-smoke",
        "name": "MMLU 高中计算机科学 20 题（DSH）",
        "version": "1.0.1",
        "benchmark_type": "general",
        "description": "从公开 cais/mmlu 的 high_school_computer_science/test split 固化的 20 条无工具选择题，用于真实 DSH 小成本回归。",
        "dataset": "cases.jsonl", "pass_threshold": 80,
        "source": "https://huggingface.co/datasets/cais/mmlu", "license": "MIT", "visibility": "private",
        "source_metadata": {"dataset": DATASET, "config": CONFIG, "split": SPLIT, "offset": 0, "count": COUNT},
    }
    output.mkdir(parents=True)
    (output / "benchmark.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "cases.jsonl").write_text("".join(json.dumps(task, ensure_ascii=False) + "\n" for task in tasks), encoding="utf-8")
    (output / "README.md").write_text(
        "# MMLU 高中计算机科学 20 题（DSH）\n\n"
        "这是从公开 `cais/mmlu` 的 `high_school_computer_science/test` split 固化的前 20 条题目。\n\n"
        "- 运行时不访问网络；题目和标准答案已冻结在 `cases.jsonl`。\n"
        "- 任务不允许调用工具，最终输出必须严格为 `FINAL: A`、`FINAL: B`、`FINAL: C` 或 `FINAL: D`。\n"
        "- 它用于真实 DSH 的低成本知识回归，不能代表工具调用或长程 Agent 能力。\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the frozen 20-item MMLU DSH smoke Benchmark.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    write_package(output, fetch_rows())
    print(f"Wrote {COUNT} MMLU tasks to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
