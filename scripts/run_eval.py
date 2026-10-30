"""Phase 6: run approaches over all bad days (+ held-out + control), resumably, with a spend cap.

    uv run python scripts/run_eval.py --approaches none playbook
    uv run python scripts/run_eval.py --approaches plain_llm --cap-usd 8
    uv run python scripts/run_eval.py --approaches agent --cap-usd 28

Results append to data/eval/results_<approach>.jsonl; days already there are skipped, so a
crash or a second run never pays twice.
"""

from __future__ import annotations

import argparse
import json

from wfm.agent.llm import LLM, LLMUnavailable
from wfm.config import DATA_DIR
from wfm.data.scenarios import load_heldout, load_scenarios
from wfm.evaluation import APPROACHES, evaluate_day

EVAL_DIR = DATA_DIR / "eval"


def all_days() -> list[str]:
    return [s.id for s in load_scenarios(include_control=True)] + [s.id for s in load_heldout()]


def done(approach: str) -> set[str]:
    path = EVAL_DIR / f"results_{approach}.jsonl"
    if not path.exists():
        return set()
    return {json.loads(line)["scenario_id"] for line in path.read_text().splitlines() if line.strip()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--approaches", nargs="+", default=APPROACHES, choices=APPROACHES)
    ap.add_argument("--days", nargs="*", help="limit to these scenario ids")
    ap.add_argument("--cap-usd", type=float, default=30.0, help="stop starting new days past this live spend")
    ap.add_argument("--mode", default=None, help="LLM mode override (record / cached / live)")
    args = ap.parse_args()
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    llm = LLM(mode=args.mode)
    for approach in args.approaches:
        path = EVAL_DIR / f"results_{approach}.jsonl"
        for sid in args.days or all_days():
            if sid in done(approach):
                continue
            if approach in ("plain_llm", "agent") and llm.usage.cost_usd >= args.cap_usd:
                print(f"{approach}: spend cap US${args.cap_usd} reached; stopping before {sid}", flush=True)
                break
            try:
                rec = evaluate_day(approach, sid, llm if approach in ("plain_llm", "agent") else None)
            except LLMUnavailable as exc:
                print(f"{approach}: Claude unavailable on {sid} ({exc}); stopping, nothing saved for it", flush=True)
                break
            if rec["llm_errors"]:  # the day ran without its AI: not a valid result, retry next time
                print(f"{approach}: {sid} incomplete ({rec['llm_errors'][0]}); not saved; stopping", flush=True)
                break
            with path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            print(f"{approach:9} {sid:9} alert {rec['first_alert'] or '-':5} SL {rec['service_level_pct']:5.1f} "
                  f"aband {rec['abandon_rate_pct']:4.1f} cost Rs {rec['cost_inr']:>7,} diag {rec['diagnosis_first']} "
                  f"viol {len(rec['violations'])} US${rec['claude_usd']:.2f} (run total US${llm.usage.cost_usd:.2f})"
                  + (f" ERR {rec['llm_errors'][:1]}" if rec["llm_errors"] else ""), flush=True)


if __name__ == "__main__":
    main()
