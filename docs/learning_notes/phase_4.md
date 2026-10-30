# Phase 4, explained in plain language

## The big idea of Phase 4

Phase 3 taught the agent to say **what is wrong**. Phase 4 teaches it to say **what to do
about it**, get a human's OK, do it, and keep watching. It is the step from a smart alarm
to a useful colleague.

## How a decision is made (the six steps after the diagnosis)

Think of a sat-nav after a road closure:

1. **Propose routes.** Claude drafts 3-4 genuinely different plans from the menu of fixes
   (move breaks, cancel coaching, overtime, borrow cross-trained staff, IVR message or
   callback, chat). Code tells it exactly how big the hole is ("payments is 32 agents short
   at 11:00") and what each fix can deliver right now, and Claude can look up any rule.
2. **Drive each route in a simulator.** Every plan is tested in an *imagined* future: the
   real morning so far, plus the agent's own forecast of the rest of the day. It never sees
   the real future. That would be a crystal ball, and the results would be dishonest.
3. **Check the rules.** Plain code checks every plan against the 27 policy rules and the
   supervisor's limits, quoting rule IDs ("MOV-03: general would drop to 37%").
4. **Pick the route.** Code, not the AI, picks the cheapest plan that brings service level
   back to 80% during the disruption. If none does, it picks the best one available; below
   70% it calls in the duty manager. Claude writes the explanation.
5. **Ask the supervisor.** Approve, or reject with a reason. "No overtime budget today"
   becomes a hard rule for the rest of the day, and the agent re-plans immediately.
6. **Do it, then keep watching.** The approved plan is applied to the *real* day. Every 15
   minutes the agent checks whether things are on track. If the disruption lasts longer
   than expected, or new evidence appears, it re-plans. When things are calm for 30
   minutes, it stands down.

## Results: five different bad days, real outcomes

Service level from the first alert to close (callers answered within 20 seconds):

| Bad day | Nobody acts | With the agent | Callers who hung up |
|---|---|---|---|
| UPI outage (demo day) | 57.5% | **85.2%** | 14.4% -> 4.2% |
| Marketing campaign | 62.3% | **86.3%** | 9.1% -> 3.1% |
| Sick-call wave | 61.7% | **81.4%** | 9.7% -> 4.4% |
| Slow computer systems | 64.0% | **82.9%** | 9.4% -> 4.1% |
| Cyclone-warning surge | 49.9% | **65.6%** | 13.1% -> 8.1% |
| UPI outage, supervisor says "no overtime" | 57.5% | **77.2%** with zero overtime | 14.4% -> 5.6% |

- The weather day is the hard one. Both phone lines are swamped at once, so there is nobody
  to borrow, and the agent rightly escalated to the duty manager several times.
- Rejecting overtime still worked. The agent switched to free fixes (outage message, chat,
  callback, break moves) and got most of the way, at about Rs 2,800 instead of Rs 16,700.
  That trade-off is exactly what a supervisor wants to see.
- **Cost of AI per full day:** about US$0.75-1.00 (4-5 planning rounds).
- **Time per planning round:** typically 25-75 seconds.

## Honest lessons from this phase

1. **A calm afternoon can hide a crisis.** The first version judged plans on the whole rest
   of the day and once recommended "no action" while service level was at 33%. Plans are
   now judged on the disruption window itself.
2. **"Expected" is not "guaranteed".** Overtime was planned on the 40% of volunteers we
   *expect* to accept. On two days more said yes, and real spend reached Rs 17,280 against
   a Rs 15,000 budget. Real WFM teams use first-come, first-served slots, so now does our
   simulator: once the budget is booked, later volunteers are thanked and declined.
3. **Rules the planner cannot see should be enforced by the machine.** The planner could
   not see which agents had already had a break delayed, so it kept proposing plans that
   broke the "one delayed break per agent" rule. The executor now simply skips those agents.
4. **The demo must never crash.** If the AI is unreachable or the account runs out of credit
   (which happened during this phase), the day continues safely and the duty manager is
   alerted.

## How to see it yourself
- `docs/phase4_results.md`: every day's full trace (diagnosis, options, scores, rule checks,
  approvals, actions, monitoring).
- `runs/<day>/day-*.jsonl`: the audit log of each day.
- `make agent-days`: re-runs the five days and the rejection demo (free now, replayed).
- `make test`: 63 checks, all passing.
