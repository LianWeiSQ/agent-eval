from __future__ import annotations

import json
import sys


payload = json.load(sys.stdin)
final_output = payload.get("agent_run", {}).get("final_output")
text = final_output if isinstance(final_output, str) else json.dumps(final_output, ensure_ascii=False)
keyword = str(payload.get("task", {}).get("expected_keyword", "hello-eval-v1"))
passed = keyword in text
json.dump(
    {
        "score": 100 if passed else 0,
        "passed": passed,
        "reason": f"输出{'包含' if passed else '不包含'}必需关键词：{keyword}",
        "evidence_refs": ["agent_run.final_output"],
        "hard_failures": [],
    },
    sys.stdout,
    ensure_ascii=False,
)
