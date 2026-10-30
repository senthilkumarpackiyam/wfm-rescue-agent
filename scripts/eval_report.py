"""Phase 6: build docs/eval_report.md and charts from data/eval/*.jsonl.

Run:  uv run python scripts/eval_report.py
"""

from __future__ import annotations

import json
import statistics as st

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from judge_explanations import JUDGE_FILE, SHEET, agreement
from phase1_overview import AXIS, GRID, INK, INK_2, MUTED, SURFACE
from wfm.config import DATA_DIR, ROOT

EVAL_DIR = DATA_DIR / "eval"
OUT = ROOT / "docs" / "eval_report.md"
IMG = ROOT / "docs" / "img"
NAMES = {"none": "No action", "playbook": "Rule-based playbook", "plain_llm": "Plain LLM", "agent": "Full agent"}
COLOR = {"agent": "#2a78d6", "none": "#eb6834", "playbook": "#1baf7a", "plain_llm": "#eda100"}
TYPES = {"OUT": "Outage", "MKT": "Marketing", "WTH": "Weather / seasonal", "SCK": "Sick-call wave",
         "AHT": "Slow systems", "ADV": "Adversarial"}
SIDE = {"outage": "demand", "marketing_campaign": "demand", "unknown_demand_surge": "demand",
        "sick_wave": "supply", "system_slowness": "supply", "unknown_supply_issue": "supply", "none": "none"}


def load() -> dict[str, dict[str, dict]]:
    out = {}
    for a in NAMES:
        path = EVAL_DIR / f"results_{a}.jsonl"
        out[a] = {json.loads(l)["scenario_id"]: json.loads(l) for l in path.read_text().splitlines()} if path.exists() else {}
    return out


def summarise(res, days: list[str]) -> dict[str, dict]:
    none = res["none"]
    rows = {}
    for a in NAMES:
        r = [res[a][d] for d in days if d in res[a]]
        if not r:
            continue
        gains = [x["service_level_pct"] - none[x["scenario_id"]]["service_level_pct"] for x in r]
        costs = [x["cost_inr"] for x in r]
        plans = sum(x["plans_applied"] for x in r)
        bad_plans = sum(1 for x in r for _ in set(x["violations"])) if plans else 0
        diag = [x for x in r if x["diagnosis_first"] is not None]
        adv = [x for x in r if x["type"] == "adversarial"]
        times = [x["time_to_recommendation_s"] for x in r if x["time_to_recommendation_s"] is not None]
        rows[a] = {
            "days": len(r),
            "sl": st.mean(x["service_level_pct"] for x in r),
            "abandon": st.mean(x["abandon_rate_pct"] for x in r),
            "cost": st.mean(costs),
            "gain": st.mean(gains),
            "cost_per_point": (st.mean(costs) / st.mean(gains)) if st.mean(gains) > 0.5 else None,
            "diag_exact": (sum(x["diagnosis_first"] == x["expected_diagnosis"] for x in diag), len(diag)),
            "diag_side": (sum(SIDE.get(x["diagnosis_first"]) == SIDE[x["expected_diagnosis"]] for x in diag), len(diag)),
            "plans": plans,
            "violations": sum(len(x["violations"]) for x in r),
            "violation_rules": sorted({v for x in r for v in x["violations"]}),
            "bad_plan_days": sum(1 for x in r if x["violations"]),
            "donor_breaches": sum(x["donor_breaches"] for x in r),
            "obeyed": (sum(bool(x["injection_obeyed"]) for x in adv), len(adv)),
            "time": st.median(times) if times else None,
            "time_complete": all(x.get("timing_complete", True) for x in r if x["time_to_recommendation_s"] is not None),
            "usd": sum(x.get("claude_usd_total", x.get("claude_usd", 0)) for x in r),
            "wins_vs_playbook": sum(res[a][d]["service_level_pct"] > res["playbook"][d]["service_level_pct"]
                                    for d in days if d in res[a] and d in res["playbook"]) if a != "playbook" else None,
        }
    return rows


def table(rows: dict, note_time: str) -> list[str]:
    out = ["| Metric | " + " | ".join(NAMES[a] for a in rows) + " |", "|---" * (len(rows) + 1) + "|"]

    def line(label, fn):
        out.append(f"| {label} | " + " | ".join(fn(a, r) for a, r in rows.items()) + " |")

    line("Service level, first alert to close", lambda a, r: f"**{r['sl']:.1f}%**")
    line("Gain over no action", lambda a, r: f"{r['gain']:+.1f} pts")
    line("Callers who hung up", lambda a, r: f"{r['abandon']:.1f}%")
    line("Cost of actions per day", lambda a, r: f"₹{r['cost']:,.0f}")
    line("Cost per service-level point", lambda a, r: f"₹{r['cost_per_point']:,.0f}" if r["cost_per_point"] else "-")
    line("Diagnosis exactly right", lambda a, r: f"{r['diag_exact'][0]}/{r['diag_exact'][1]}" if r["diag_exact"][1] else "-")
    line("Diagnosis on the right side (demand / supply)",
         lambda a, r: f"{r['diag_side'][0]}/{r['diag_side'][1]}" if r["diag_side"][1] else "-")
    line("Policy violations (rule breaks in applied plans)", lambda a, r: f"{r['violations']}"
         + (f" ({', '.join(r['violation_rules'])})" if r["violation_rules"] else "") if a != "none" else "-")
    line("Moves where the other queue fell below 70%", lambda a, r: str(r["donor_breaches"]) if a != "none" else "-")
    line("Hidden instructions obeyed", lambda a, r: f"{r['obeyed'][0]}/{r['obeyed'][1]}"
         if r["obeyed"][1] and a != "none" else "-")
    line(f"Time from alert to recommendation (median){note_time}",
         lambda a, r: ("instant" if a == "playbook" else f"{r['time']:.0f} s" + ("" if r["time_complete"] else "*"))
         if r["time"] is not None else "-")
    line("Days with better service level than the playbook",
         lambda a, r: f"{r['wins_vs_playbook']}/{r['days']}" if r["wins_vs_playbook"] is not None else "-")
    line("Claude cost per day (US$)", lambda a, r: f"{r['usd'] / r['days']:.2f}" if a in ("plain_llm", "agent") else "0")
    return out


def by_type(res, days) -> tuple[list[str], dict]:
    data = {}
    for t, label in TYPES.items():
        ds = [d for d in days if d.startswith(t)]
        data[label] = {a: st.mean(res[a][d]["service_level_pct"] for d in ds if d in res[a]) for a in NAMES
                       if any(d in res[a] for d in ds)}
    out = ["| Kind of day | " + " | ".join(NAMES.values()) + " |", "|---" * 5 + "|"]
    for label, v in data.items():
        best = max(v, key=v.get)
        out.append(f"| {label} | " + " | ".join((f"**{v[a]:.1f}%**" if a == best else f"{v[a]:.1f}%") if a in v else "-"
                                              for a in NAMES) + " |")
    return out, data


def charts(type_data: dict, rows: dict) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.2), facecolor=SURFACE, gridspec_kw={"width_ratios": [2.2, 1]})
    ax = axes[0]
    labels = list(type_data)
    x = np.arange(len(labels))
    w = 0.2
    for i, a in enumerate(NAMES):
        ax.bar(x + (i - 1.5) * (w + 0.01), [type_data[l].get(a, 0) for l in labels], w, color=COLOR[a], label=NAMES[a])
    ax.axhline(80, color=AXIS, linewidth=1, linestyle="--")
    ax.set_xticks(x, labels, fontsize=9)
    ax.set_ylim(0, 100)
    ax.set_title("Service level from first alert to close, by kind of bad day", loc="left", fontsize=12,
                 color=INK, fontweight="bold")
    ax.legend(frameon=False, fontsize=9, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.08), labelcolor=INK_2)
    ax = axes[1]
    for a, r in rows.items():
        ax.scatter(r["cost"], r["sl"], s=140, color=COLOR[a], zorder=3)
        ax.annotate(NAMES[a], (r["cost"], r["sl"]), textcoords="offset points", xytext=(8, -4), fontsize=9, color=INK_2)
    ax.set_title("Average cost vs service level (30 days)", loc="left", fontsize=12, color=INK, fontweight="bold")
    ax.set_xlabel("Cost of actions per day (₹)", color=MUTED)
    ax.set_ylabel("Service level %", color=MUTED)
    ax.set_xlim(-1000, max(r["cost"] for r in rows.values()) * 1.45)
    for a_ in axes:
        a_.set_facecolor(SURFACE)
        a_.grid(axis="y", color=GRID, linewidth=0.8)
        a_.set_axisbelow(True)
        for side in ("top", "right"):
            a_.spines[side].set_visible(False)
        a_.tick_params(colors=MUTED, labelsize=8, length=0)
    fig.tight_layout(w_pad=3)
    fig.savefig(IMG / "eval_summary.png", dpi=140, facecolor=SURFACE)


def judge_section() -> list[str]:
    if not JUDGE_FILE.exists():
        return ["Explanation quality not graded yet (run `scripts/judge_explanations.py`)."]
    rows = [json.loads(l) for l in JUDGE_FILE.read_text().splitlines()]
    out = ["| Explanations graded by the LLM judge (1-5) | " + " | ".join(
        ["Clear", "Specific", "Honest", "Trade-offs", "Actionable", "**Overall**", "n"]) + " |", "|---" * 8 + "|"]
    for a in ("agent", "plain_llm"):
        r = [x for x in rows if x["approach"] == a]
        if r:
            out.append(f"| {NAMES[a]} | " + " | ".join(f"{st.mean(x[c] for x in r):.1f}" for c in
                                                       ["clear", "specific", "honest", "tradeoffs", "actionable"])
                       + f" | **{st.mean(x['overall'] for x in r):.1f}** | {len(r)} |")
    ag = agreement() if SHEET.exists() else {"graded": 0}
    out += ["", (f"**Judge check:** the team graded {ag['graded']} agent recommendations blind; the judge gave the same "
                 f"overall score {ag['exact_agreement_pct']}% of the time and was within one point "
                 f"{ag['within_one_point_pct']}% of the time (team mean {ag['human_mean']}, judge mean {ag['judge_mean']}).")
            if ag.get("graded") else
            "**Judge check:** pending. The team grades the 20 recommendations in `docs/eval/label_sheet.csv` "
            "(column `your_overall_score_1_to_5`), then `scripts/judge_explanations.py --agreement` reports agreement."]
    return out


def main() -> None:
    res = load()
    main_days = sorted(d for d in res["none"] if "-H" not in d and d != "NORMAL-01")
    held = sorted(d for d in res["none"] if "-H" in d)
    rows = summarise(res, main_days)
    rows_h = summarise(res, held)
    type_lines, type_data = by_type(res, main_days)
    charts(type_data, rows)
    calm = {a: res[a].get("NORMAL-01", {}).get("actions_applied") for a in NAMES}
    lines = [
        "# Evaluation report: four approaches, 30 bad days + 6 held-out days",
        "",
        "Generated by `uv run python scripts/eval_report.py` from `data/eval/`. Every approach faces the",
        "identical day (same callers, same seed). Outcomes are measured on the real simulated day from the",
        "first alert to close; the first alert is the same for every approach (shared detector).",
        "",
        "- **No action:** the morning plan runs unchanged.",
        "- **Rule-based playbook:** fixed if-then rules (breaks, coaching, callback, overtime list, borrow agents,",
        "  known-issue message when a P1/P2 ticket is open). No AI, no simulator.",
        "- **Plain LLM:** the same Claude model, the same evidence and lever menu, one request per round with",
        "  no tools, no simulator and no policy checker; its plan is applied as written.",
        "- **Full agent:** tools, projected-day simulator, policy RAG and checks, refinement, monitoring;",
        "  auto-approved here (the human approval flow is shown in the demo).",
        "",
        "## Headline: the 30 bad days",
        "",
        *table(rows, ""),
        "",
        "\\* Agent times on these days exclude the diagnosis step (about 12 s, measured live in Phase 3), whose saved",
        "answers predate time-recording.",
        "",
        "## Held-out: 6 fresh days the agent was never tuned on",
        "",
        "New dates, a new seed, new kinds of campaign and surge, and a planted instruction in a *calendar entry*",
        "worded differently from anything seen before.",
        "",
        *table(rows_h, ""),
        "",
        "## By kind of bad day (30 days)",
        "",
        *type_lines,
        "",
        "![Evaluation summary](img/eval_summary.png)",
        "",
        "## Control day",
        "",
        "On the ordinary control day (NORMAL-01) the detector never fires, so no approach changes anything: "
        + ", ".join(f"{NAMES[a]} {calm[a] or 0} actions" for a in NAMES) + ".",
        "",
        "## Explanation quality",
        "",
        *judge_section(),
        "",
        "## Every day",
        "",
        "| Day | First alert | " + " | ".join(NAMES.values()) + " | Agent diagnosis | Agent cost (₹) |",
        "|---" * 8 + "|",
    ]
    for d in main_days + held:
        cells = [f"{res[a][d]['service_level_pct']:.1f}%" if d in res[a] else "-" for a in NAMES]
        ag = res["agent"].get(d)
        mark = "" if not ag else ("" if ag["diagnosis_first"] == ag["expected_diagnosis"] else " ✗")
        lines.append(f"| {d} | {res['none'][d]['first_alert']} | " + " | ".join(cells)
                     + f" | {(ag or {}).get('diagnosis_first', '-')}{mark} | {(ag or {}).get('cost_inr', 0):,} |")
    worst = sorted((d for d in main_days + held if d in res["agent"]),
                   key=lambda d: res["agent"][d]["service_level_pct"] - res["playbook"][d]["service_level_pct"])[:3]
    lines += ["", "## Where the agent loses", "",
              "The days where the agent is furthest behind the playbook. The full analysis (two faults in the",
              "re-forecast that make the agent under-react to broad surges, and the designed fix) is in",
              "[failure_case.md](failure_case.md).", ""]
    for d in worst:
        a, p = res["agent"][d], res["playbook"][d]
        lines.append(f"- **{d}**: agent {a['service_level_pct']:.1f}% at ₹{a['cost_inr']:,} vs playbook "
                     f"{p['service_level_pct']:.1f}% at ₹{p['cost_inr']:,}.")
    audited = [r for a in ("plain_llm", "agent") for r in res[a].values() if "claude_usd_total" in r]
    if audited:
        lines += ["", "## Reproducibility", "",
                  f"`scripts/replay_audit.py` re-ran all {len(audited)} AI days from the saved Claude answers: every",
                  "service level and rupee cost reproduced exactly, at no Claude cost. \"Claude cost per day\" above is",
                  "what the saved answers cost to produce."]
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} and docs/img/eval_summary.png")


if __name__ == "__main__":
    main()
