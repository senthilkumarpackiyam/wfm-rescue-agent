"""Phase 6: replay every AI day from saved Claude answers (free) to (1) prove each result
reproduces exactly and (2) record what the answers cost to produce.

Run after scripts/run_eval.py:  uv run python scripts/replay_audit.py
"""

from __future__ import annotations

import json

from wfm.agent.llm import LLM
from wfm.config import DATA_DIR
from wfm.evaluation import evaluate_day

EVAL_DIR = DATA_DIR / "eval"


def main() -> None:
    mismatches = []
    for approach in ("plain_llm", "agent"):
        path = EVAL_DIR / f"results_{approach}.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for rec in records:
            llm = LLM(mode="cached")
            again = evaluate_day(approach, rec["scenario_id"], llm)
            rec["claude_usd_total"] = round(llm.replayed.cost_usd, 3)
            same = (again["service_level_pct"], again["cost_inr"]) == (rec["service_level_pct"], rec["cost_inr"])
            if not same or again.get("llm_errors"):
                mismatches.append(f"{approach}:{rec['scenario_id']}")
            print(f"{approach:9} {rec['scenario_id']:9} reproduced={same} cost to produce US${rec['claude_usd_total']}",
                  flush=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
    print("all results reproduce exactly" if not mismatches else f"MISMATCHES: {mismatches}")


if __name__ == "__main__":
    main()
