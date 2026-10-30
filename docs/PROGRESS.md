# Progress

| Phase | Focus | Checkpoint | Status |
|---|---|---|---|
| 1 | Scope freeze, interfaces, synthetic history, scenario list | Agreed data formats; 30 scenarios with true causes | **Done** 2026-10-28 |
| 2 | Simulator v1, Erlang A, SARIMA forecast, morning roster | Normal day + outage day replay in under 2 minutes | **Done** 2026-10-28 (0.07 s per day) |
| 3 | Agent loop: detect, diagnose, re-forecast; feed tools; policy RAG | Agent diagnoses the outage scenario end to end | **Done** 2026-10-29 (30/30 diagnosed, [results](phase3_results.md)) |
| 4 | Options, what-if simulation, policy check, recommend, approve / re-plan | Recovers service level on 5 scenarios; reject-and-re-plan works | **Done** 2026-10-30 (5/5 recovered, reject-and-re-plan works, [results](phase4_results.md)) |
| 5 | Streamlit split-screen dashboard, trace panel, guardrails, logging | Full demo run on the outage scenario without errors | **Done** 2026-10-30 (automated click-through passes; live mode checked) |
| 6 | Full eval, four approaches, fix worst failures | Final metrics table + one documented failure case | **Done** 2026-10-30 ([report](eval_report.md), [failure case](failure_case.md)) |
| 7 | Slides, cached-response mode, rehearsals, backup video | Submission sent | **Ready** 2026-10-30: deck, offline proof, 3 rehearsals, backup video, zip. Sending is the lead's step |

## Decisions log (scope freeze, Phase 1)

| Decision | Choice | Why |
|---|---|---|
| Operating hours | 08:00-20:00, 7 days, 15-min intervals (48/day) | Typical bank contact centre; matches the agent's 15-min loop |
| Queues | `payments` (Payments & UPI), `general` (Accounts & General); 25% of agents cross-skilled | Two queues make "move cross-skilled agents" real and show donor-queue harm |
| Service level | 80% answered in 20 s | Industry-standard target |
| Size | ~3,100 + ~2,350 calls/day; peak ~100 calls / 15 min on payments | Big enough to be realistic, small enough to simulate in seconds |
| History | 2025-01-01 to 2026-09-30, synthetic | Enough seasons for SARIMA; scenarios sit in Oct-Nov 2026 |
| Scenario dates | Distinct Mon-Sat dates, 2026-10-01 to 2026-11-07 | Each bad day has its own baseline; OUT-01 (demo) pinned to Tue 2026-10-06, 10:20 |
| Weather/seasonal | Correct diagnosis is `unknown_demand_surge` | The plan tests "cause unknown" handling; nothing in the feeds explains these days |
| Sick waves | Evidence is the attendance log, generated with the roster in Phase 2 | Absences only make sense once real agents exist |
| Money | INR; agent Rs 180/h, overtime 2x = Rs 360/h, OT budget Rs 15,000/day | Indian contact-centre context from the plan |
| LLM (proposed) | Claude via the Anthropic API, structured outputs | "One frontier model via API"; needs an API key by Phase 3 |

## Decisions log (Phase 2)

| Decision | Choice | Why |
|---|---|---|
| Forecast model | SARIMA (0,1,1)(0,1,1,7) on log daily totals, last 364 days; intraday shape and AHT from the last 8 same weekdays | Best of four candidates on a September backtest: daily error 2.9% (payments) / 4.5% (general) |
| Forecast issue date | One forecast issued from history ending 30 Sep covers all scenario days | Mirrors schedules published weeks ahead; avoids refitting 31 times |
| Caller patience | 180 s payments, 240 s general (was 90/150) | 90 s gave ~8% abandonment at target, unrealistic for a bank; now 2-4% |
| Handle-time spread | Coefficient of variation 0.95 / 0.90 (was 0.55 / 0.50) | Real call lengths are long-tailed |
| Roster method | Greedy shift cover (FT 8.5 h, PT 4.5 h), breaks and coaching placed where slack is largest | An exact optimiser (scipy MILP) only trimmed ~1.5 agents per slot; greedy is simpler and deterministic |
| Planning target | Roster planned to Erlang A 65% | Erlang A ignores cross-skilled agents floating between queues, so 80% overstaffed (normal days at 97%). At 65%, 31 simulated normal days average 91% and none falls below 80% |
| What-if method | Re-run the whole day with actions applied from decision time | All callers are pre-drawn from the seed, so the past replays identically (tested) |
| Callbacks in SL | Callers who accept a callback leave the SL denominator; the callback work still loads agents later | Common industry convention; policy caps keep it from being gamed |
| Shock sizes | Weather 1.18-1.36x, sick waves 14-22%, slowdowns 1.55-1.85x | After calibration all 30 days fail the 80% promise with no action; 25 can be lifted 10+ points ([calibration](calibration.md)) |

## Decisions log (Phase 3)

| Decision | Choice | Why |
|---|---|---|
| Model | `claude-opus-5-5`, adaptive thinking (always on), effort `medium` | Default current model; medium was accurate (30/30) at about 4.7 US cents per diagnosis |
| Refusal fallback | Server-side `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) | A rare safety decline is retried on Anthropic's recommended model instead of stalling the live loop. Can be switched off in `wfm/agent/llm.py` |
| Diagnosis design | Claude calls 5 evidence tools, then returns JSON in a fixed schema; code checks every cited ID exists and that the evidence type fits the cause; one correction round, then downgrade to "unknown" | Evidence is mandatory (plan guardrail) and enforced by code, not by trust |
| Prompt injection | Plain-code pattern scanner + the model's own flags, combined; feed text labelled untrusted | Defence in depth: both adversarial days flagged, none obeyed, no false flags |
| Detector | Gap must be large (%) *and* unlikely to be noise (z-score >= 4); 30-min and 90-min views; service level only as supporting signal; staffing >= 15% and >= 5 agents; attendance-log check | Fixed % thresholds gave false alarms on all 31 calm days; now 0 of 31, all 30 bad days caught, median 38 min after the disruption starts (spikes ~25 min, slow weather surges 75-180 min) |
| Re-forecast | Shock factor x cause-specific shape: outage holds 45 min then fades over 60; campaign holds 3 h then fades over 2 h; unexplained surge holds to close; slowdown holds 60 then fades 60; resolved incident fades over 30 | Numbers stay in code; the diagnosis only picks the shape |
| Policy RAG | 27 rules in 6 documents; Chroma (MiniLM embeddings) + BM25 keywords fused by reciprocal rank | Plan's "hybrid search"; rules are chunked by ID so answers can cite them |
| LLM cache | Record/replay keyed by the exact request; `WFM_LLM_MODE=record/cached/live` | Free re-runs and tests; `cached` is the plan's offline demo mode |
| Prompt revision | Added one line of domain knowledge ("IVR menus are coarse; campaign calls arrive under generic reasons") after two marketing days were called "unknown" | Raised 26/30 to 30/30 without new errors. **Tuned on the test set**, so Phase 6 adds fresh held-out scenarios for an honest score |

Claude spend so far: about US$2.90 (two full 31-day runs plus tests).

## Decisions log (Phase 4)

| Decision | Choice | Why |
|---|---|---|
| What-if world | Plans are simulated on the agent's *projected* day: real past up to now + future callers drawn from its own re-forecast + only absences already logged | Simulating on the scenario's real future would be clairvoyance; only the approved plan runs against reality (tested) |
| Who chooses | Code: the cheapest policy-compliant plan that reaches 80% over the disruption window; otherwise the best compliant plan; escalate below 70% (ESC-02). Claude only explains | Auditable choice; the LLM writes, code decides |
| Judging window | From now until the projected staffing gap closes (at least 2 h), not the whole rest of the day | First runs showed a calm afternoon hiding a 33% service level happening *now* |
| Refinement | If no compliant plan reaches 80%, Claude gets one more round with the simulator and policy results | Showed real value: R1/R2 plans fixed MOV-03 violations on OUT-01 and MKT-01 |
| BRK-04 | Executor skips agents whose break was already delayed today (like mandatory training) | Planner cannot see individual agents; repeated violations wasted rounds |
| Planner context | Code supplies gap per hour, predicted SL per hour if nothing changes, what each lever can deliver now, OT budget left | Plans are sized on numbers, not guesses |
| Supervisor | Approve / edit / reject with a reason; Claude turns the reason into structured constraints that code enforces for the rest of the day | Plan's "plain-English constraints" requirement |
| Monitoring | Re-assess 45+ min after a decision if: new evidence, recent SL < 70% on the disrupted queue, or the plan has ended; close the episode after 30 quiet minutes; max 5 planning rounds a day | Handles disruptions that outlast the plan (OUT-01's outage ran 3+ hours) |
| Outages of the AI | If Claude is unreachable or out of credit, the day continues unchanged and the duty manager is alerted | A live demo must never crash mid-day |
| Overtime budget in reality | Executor books overtime slots first come, first served and declines volunteers once Rs 15,000 is booked (OT-04 text updated) | Planning on *expected* acceptance let real spend reach Rs 17,280 on two days; now spend can never exceed the cap |
| Bug found | A very long call running far past close crashed the busy-time bookkeeping in projected days; fixed with a regression test | Found by the first full checkpoint run |

## Decisions log (Phase 5)

| Decision | Choice | Why |
|---|---|---|
| Human in the loop | `DaySession(supervisor=None)` holds the recommendation and waits for `decide()`; the clock stops while a person decides | The demo's "human moment" needs a real pause, not an auto-approver |
| Live trace | The graph is streamed node by node (`stream_mode="updates"`); each step appears as it finishes, with an optional pause for the audience | Plan: "the trace panel streams detection, diagnosis, re-forecast, options" |
| Testability | All demo logic lives in `wfm/ui/controller.py`; the Streamlit page only draws it; Streamlit's AppTest clicks through the whole demo in CI | Checkpoint "full demo run without errors" is an automated test, not a manual check |
| Edits | "Untick anything you do not want" on the recommended plan = approve with edits; the edited plan is policy-checked before it is applied | Plan's approve / edit / reject |
| Look | Light theme, developer toolbar hidden, large trace font, reference chart palette (blue = agent, orange = no agent) | Readable from the back of the room |
| Offline safety | "AI answers" switch: saved answers with live fallback (default), offline only, or always live; bookmarkable URL parameters | Plan's live/cached toggle and safety net |
| Demo timing | Offline: alert-to-recommendation ~10 s with audience pauses; live: ~57 s (OUT-02 test, US$0.19) | Fits the pitch's 90-second "agent works live" slot either way |
| Cost so far | US$11.65 in total after Phase 4 (297 Claude answers, all cached); ~US$0.75-1.00 per full agent day | Lead chose option A (Opus 5.5 throughout) on 2026-10-30 |

## Decisions log (Phase 6)

| Decision | Choice | Why |
|---|---|---|
| Comparison approaches | No action; rule-based playbook (fixed if-then rules, no AI); plain LLM (same Claude model, same evidence and lever menu, no tools, simulator or checks); full agent (auto-approved) | The plan's four approaches. The plain LLM isolates what the tools, simulator and checks add over the model alone |
| Fair start | All four share the detector, so the first alert is identical; outcomes are measured on the real day from that alert to close | Nothing differs before the first alert, so measuring the whole day would dilute the differences |
| Held-out days | 6 fresh days (new dates 9-28 Nov, new seed, new campaign and surge kinds, a planted instruction inside a calendar entry worded to slip past the code scanner) | Phase 3's prompt was tuned on the 30 test days; held-out days give an honest score |
| Rule breaks | Applied plans re-checked with the agent's own policy checker; donor queue measured on the real day (MOV-03) | Same yardstick for every approach |
| Explanation quality | LLM judge (5 criteria + overall, blind to the approach) on each day's first recommendation; 20 agent recommendations exported for the team to grade, agreement computed afterwards | A judge is cheap but must be checked against people |
| Spend control | Resumable runs, a dollar cap, and incomplete days (any Claude error) are never saved | A crash or a second run never pays twice and never records a half-run day |
| Reproducibility | All 74 AI days replayed from saved answers: every result reproduced exactly at US$0 | Results can be re-checked by anyone with the folder, without an API key |
| Results | 30 days: agent 82.8% service level, plain LLM 78.4%, playbook 74.3%, no action 64.9%; agent 0 rule breaks, 30/30 diagnoses, ₹692 per service-level point. Held-out: 82.2% / 74.9% / 73.3% / 65.1%. Judge: agent 4.3/5, plain LLM 3.9/5 | [eval_report.md](eval_report.md) |
| Failure case | Weather and seasonal surges: agent 75.0% vs plain LLM 83.0%. Two re-forecast faults: only the alarming queue is adjusted; an unrelated closed ticket faded the surge on WTH-04 | [failure_case.md](failure_case.md). The fix is designed but **not applied**: the headline numbers stay those of the frozen system, and the fix was found on these same days |
| Cost | Phase 6 about US$24.85 (agent US$20.16 for 37 days, plain LLM US$4.06, judge US$0.63). Project total US$36.70 for 882 saved answers | Within the US$43 balance the lead reported before Phase 6 |

## Decisions log (Phase 7)

| Decision | Choice | Why |
|---|---|---|
| Slide deck | 8 talk slides plus 4 appendix slides, with speaker notes and timings: [private deck link](https://claude.ai/artifact/STMjXc6pzrUkPM5ifTYxAs) (share it, or download it as PowerPoint or PDF, from its page) | The plan's script is mostly live demo; slides frame the problem, the proof and the close |
| Pitch path | Bookmark `?scenario=OUT-01&start=10:00&mode=cached`; Play at 1 simulated minute per second reaches the 10:45 alert in 45 s, matching the plan's 0:45–1:30 slot | One click starts the demo; nothing needs setting on stage |
| Offline proof | `make offline-check`: removes the key, blocks any Anthropic client, then plays the pitch path and all 31 picker days from saved answers. All pass, with 0 live calls | Proves the cached-response mode the plan asks for, and that the "crowd moment" works for any day |
| Rehearsals | `make rehearse`: three automated runs in Chrome, all OK. Machine time is about 1 min 50 s (play to recommendation 55–56 s, re-plan 12 s, run to end 37 s with 3 approvals) | About 2 minutes of the 3 min 45 s demo slot is left for talking |
| Dashboard fixes from rehearsal | The trace no longer blinks on the 1-second refresh; both sides' charts stay aligned when the agent side shows delta badges | Found by frame-by-frame checks of the recording |
| Backup video | `media/demo_backup.mp4`: 1920×1080, 3 min 6 s, captioned, 8.8 MB, recorded with Playwright in installed Chrome | The plan's safety net if the live demo fails |
| Submission | `make package` builds `dist/wfm-rescue-agent.zip` (never `.env` or `.venv`); the presenter script is in `docs/pitch_script.md` | One file to send |
| Cost | Phase 7 used US$0 of Claude; the project total stays US$36.70 | Everything ran from saved answers |

## Open items for the lead

- [x] Provide `ANTHROPIC_API_KEY` (done 2026-10-29).
- [x] Add Anthropic credit and choose a cost setting (done 2026-10-30: option A, Opus 5.5 throughout).
- [ ] Choose tracing: Langfuse or LangSmith (both need a free account), or local JSONL logs only.
- [x] Put the project on GitHub (2026-10-30, public: https://github.com/senthilkumarpackiyam/wfm-rescue-agent).
- [ ] Team: grade the 20 recommendations in `docs/eval/label_sheet.csv` (column `your_overall_score_1_to_5`, 1-5), then run `uv run python scripts/judge_explanations.py --agreement`.
- [x] Decide whether to apply the failure-case fix before submission (2026-10-30: ship as is; the fix is documented in [failure_case.md](failure_case.md)).
- [ ] Fill in `[team names]` and `[date]` on deck slides 1 and 8, then share the deck (it is private).
- [ ] Rehearse the talk out loud at least once with [pitch_script.md](pitch_script.md).
- [ ] Submit: `make package`, then send `dist/wfm-rescue-agent.zip`, the deck and `media/demo_backup.mp4`.
