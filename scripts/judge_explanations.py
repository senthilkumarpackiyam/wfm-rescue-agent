"""Phase 6: an LLM judge grades explanation quality; humans grade a sample to check the judge.

    uv run python scripts/judge_explanations.py            # grade (resumable) + write the human sheet
    uv run python scripts/judge_explanations.py --agreement # after the team fills docs/eval/label_sheet.csv

The judge sees only the text a supervisor would read, never which approach wrote it.
"""

from __future__ import annotations

import argparse
import csv
import json
import random

from wfm.agent.llm import LLM
from wfm.config import DATA_DIR, ROOT

EVAL_DIR = DATA_DIR / "eval"
JUDGE_FILE = EVAL_DIR / "judge.jsonl"
SHEET = ROOT / "docs" / "eval" / "label_sheet.csv"
CRITERIA = ["clear", "specific", "honest", "tradeoffs", "actionable"]

RUBRIC = """You grade the explanation an intraday workforce-management assistant gave a contact-centre shift supervisor before they approve a change. Score each criterion from 1 (poor) to 5 (excellent):
- clear: a busy supervisor understands it in under a minute.
- specific: it uses concrete numbers and names its evidence (incident IDs, call reasons, times) instead of vague claims.
- honest: it states uncertainty, risks or the need to escalate where appropriate, and does not overclaim.
- tradeoffs: it says what the plan costs or risks compared with the alternatives.
- actionable: the supervisor knows exactly what will be done and what to watch.
Then give an overall score from 1 to 5 (your holistic judgement, not an average) and one short sentence of justification."""

FORMAT = {"type": "json_schema", "schema": {
    "type": "object", "additionalProperties": False,
    "required": CRITERIA + ["overall", "comment"],
    "properties": {**{c: {"type": "integer", "enum": [1, 2, 3, 4, 5]} for c in CRITERIA},
                   "overall": {"type": "integer", "enum": [1, 2, 3, 4, 5]}, "comment": {"type": "string"}}}}


def items() -> list[dict]:
    """First applied recommendation per day, for the agent and the plain LLM."""
    out = []
    for approach in ("agent", "plain_llm"):
        path = EVAL_DIR / f"results_{approach}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            r = json.loads(line)
            if r["explanations"]:
                e = r["explanations"][0]
                out.append({"key": f"{approach}:{r['scenario_id']}:{e['at']}", "approach": approach,
                            "scenario_id": r["scenario_id"], "at": e["at"], "text": e["text"]})
    return out


def grade(llm: LLM) -> None:
    done = set()
    if JUDGE_FILE.exists():
        done = {json.loads(line)["key"] for line in JUDGE_FILE.read_text().splitlines()}
    for it in items():
        if it["key"] in done:
            continue
        msg = llm.create(system=RUBRIC, max_tokens=4000, output_config={"effort": "low", "format": FORMAT},
                         messages=[{"role": "user", "content": f"Explanation shown to the supervisor:\n\n{it['text']}"}])
        scores = json.loads(next(b.text for b in msg.content if b.type == "text"))
        with JUDGE_FILE.open("a") as f:
            f.write(json.dumps({**it, **scores}) + "\n")
        print(f"{it['key']:32} overall {scores['overall']} | {scores['comment'][:90]}", flush=True)


def write_sheet(n: int = 20) -> None:
    """20 agent recommendations, spread across scenario types, for the team to grade blind."""
    agent = [json.loads(line) for line in JUDGE_FILE.read_text().splitlines() if line.startswith('{"key": "agent')]
    by_type: dict[str, list] = {}
    for r in agent:
        by_type.setdefault(r["scenario_id"].split("-")[0], []).append(r)
    rng = random.Random(7)
    picked: list[dict] = []
    while len(picked) < min(n, len(agent)):
        for group in by_type.values():
            if group and len(picked) < n:
                picked.append(group.pop(rng.randrange(len(group))))
    SHEET.parent.mkdir(parents=True, exist_ok=True)
    with SHEET.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "scenario", "time", "explanation", "your_overall_score_1_to_5", "notes"])
        for i, r in enumerate(picked, 1):
            w.writerow([i, r["scenario_id"], r["at"], r["text"], "", ""])
    print(f"wrote {SHEET.relative_to(ROOT)} with {len(picked)} recommendations")


def agreement() -> dict:
    judge = {(json.loads(line)["scenario_id"], json.loads(line)["at"]): json.loads(line)["overall"]
             for line in JUDGE_FILE.read_text().splitlines() if line.startswith('{"key": "agent')}
    pairs = []
    with SHEET.open() as f:
        for row in csv.DictReader(f):
            if row["your_overall_score_1_to_5"].strip():
                pairs.append((int(row["your_overall_score_1_to_5"]), judge[(row["scenario"], row["time"])]))
    if not pairs:
        return {"graded": 0}
    exact = sum(h == j for h, j in pairs) / len(pairs)
    within1 = sum(abs(h - j) <= 1 for h, j in pairs) / len(pairs)
    return {"graded": len(pairs), "exact_agreement_pct": round(100 * exact), "within_one_point_pct": round(100 * within1),
            "human_mean": round(sum(h for h, _ in pairs) / len(pairs), 2),
            "judge_mean": round(sum(j for _, j in pairs) / len(pairs), 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agreement", action="store_true")
    args = ap.parse_args()
    if args.agreement:
        print(json.dumps(agreement(), indent=1))
        return
    llm = LLM()
    grade(llm)
    write_sheet()
    print(f"judge spend this run US${llm.usage.cost_usd:.2f}")


if __name__ == "__main__":
    main()
