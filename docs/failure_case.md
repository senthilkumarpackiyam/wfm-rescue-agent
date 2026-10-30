# Failure case: the agent under-reacts to weather and seasonal surges

The full agent wins overall (82.8% service level on the 30 bad days against 78.4% for the plain LLM and
74.3% for the playbook), but it loses on one kind of day. On weather and seasonal surges it finishes
behind the plain LLM on every day and behind the playbook on three of five. On the four main surge days
it averages 75.0% against the plain LLM's 83.0%.

| Day | No action | Playbook | Plain LLM | Full agent | Agent spend | Plain LLM spend |
|---|---|---|---|---|---|---|
| WTH-01 Heavy rain closes branches | 72.2% | 79.3% | 84.7% | 76.1% | ₹1,188 | ₹15,777 |
| WTH-02 Cyclone warning | 49.9% | 68.0% | 80.1% | 65.6% | ₹10,044 | ₹20,796 |
| WTH-03 Pre-Diwali Saturday | 66.5% | 80.7% | 85.2% | 86.4% | ₹6,828 | ₹11,172 |
| WTH-04 Month-end salary day | 70.3% | 76.8% | 82.0% | 71.7% | ₹0 | ₹12,978 |
| WTH-H1 (held-out) | 69.6% | 82.3% | 89.7% | 84.1% | ₹9,660 | ₹17,307 |

The pattern is under-spending. The agent believed the day was milder than it was, so its simulator
approved cheap plans. Two separate faults in the re-forecast code caused this. Neither is a model error.

## Fault 1 (every surge day): only the queue that raised the alarm was adjusted

A citywide surge hits both queues, but usually only one queue crosses the alarm threshold at first.
The re-forecast (`wfm/agent/reforecast.py`) adjusts a queue's calls only if that queue has its own volume
alarm. The other queue was therefore projected at the normal forecast (1.00×), even though its own
numbers already showed it running high:

| Day | Queue that alarmed: assumed vs rest of day | Other queue: assumed | Other queue: measured at the alarm | Other queue: rest of day |
|---|---|---|---|---|
| WTH-01 | payments 1.30× vs 1.29× | general 1.00× | 1.28× | 1.24× |
| WTH-02 | general 1.33× vs 1.34× | payments 1.00× | 1.14× | 1.27× |
| WTH-03 | payments 1.32× vs 1.21× | general 1.00× | 1.18× | 1.24× |
| WTH-04 | general 1.19× vs 1.25× | payments 1.00× | 1.03× | 1.18× |
| WTH-H1 | general 1.20× vs 1.34× | payments 1.00× | 1.09× | 1.13× |

The surge on the alarming queue was sized about right. The miss is the second queue.
In the projection that queue had spare people, so the simulator rated cheap plans, and borrowing
from it, as enough.

Claude itself noticed. At 14:15 on WTH-01 its recommendation reads: *"Calls are running well above
forecast in both queues (payments +27.5%, general +23.8%)"*. It still recommended no action, because
the simulator's prediction said both queues would be fine. By design (Phase 3) every number comes from
code, not from the model. That design keeps the model from making figures up, but it also means a code
blind spot cannot be overruled by the model's own reading.

## Fault 2 (WTH-04 only): an unrelated closed ticket made the surge "disappear"

At the 09:15 alarm on WTH-04, Claude correctly diagnosed an unexplained surge. In its evidence it
cited a closed IT ticket (INC-20261031-5136) that it had ruled out as the cause. The re-forecast treats
*any* cited closed ticket as "the cause is fixed, calls drop back to normal within 30 minutes". The
projection therefore showed the surge ending, the simulator predicted 98%, and the agent recommended no
action. The same thing happened at four later decision points that day. The agent never spent a rupee, it
moved two cross-skilled agents out of a payments queue that was itself overloaded, and both queues ended
at 71.7%.

A free replay of every agent day from saved answers shows that the "closed ticket" fade was used 14 times
on 8 days. Thirteen of those uses were correct: an outage whose own ticket had been closed. WTH-04 is the
only day where an unrelated ticket triggered it.

## Why the safety nets did not catch it

- **Simulator:** it faithfully simulates the day it is given. Given a projection that is too mild, it
  confirms a plan that is too small, like a flight simulator fed the wrong weather report.
- **Policy check, MOV-03 (the donor queue must stay at or above 70%):** this check runs on the projected
  day, which believed the donor queue was quiet. The agent's donor-floor breaches on the real day (7 on
  the main days, 2 on held-out) are this same kind of miss. Every one of those moves passed the check on
  the projection, but the real donor queue was busier than projected.
- **Monitoring:** it re-plans when the real service level drops under 70% or new alarms appear. On these
  days the projected window kept looking acceptable, so re-plans also came out small.

## Why the plain LLM did better on these days

It has no projection to talk it out of acting. It sees calls running 25–35% high and spends heavily,
about ₹11,000–21,000 a day. On these days, heavy spending was the right call. On most other days that
same instinct costs more for less: across all 30 days the plain LLM pays ₹982 per service-level point
against the agent's ₹692.

## The fix (designed, not applied in this evaluation)

1. **Fade only for the diagnosed cause.** Use the 30-minute "ticket closed" fade only when the diagnosis
   names that incident as the cause (an outage or system slowness). This is a one-line change.
2. **For an unexplained surge, size every queue from its own numbers.** Apply each queue's measured
   90-minute level (upward only) rather than adjusting just the queue that alarmed. An unexplained surge
   is usually broad, such as weather, festivals or salary day.
   This must stay limited to the unexplained-surge diagnosis. On calm days 10.6% of 90-minute windows
   already run 10% or more above forecast through ordinary forecast error (the 99th percentile is 1.18×),
   so trusting every queue's raw level all the time would make the agent overspend on noise.
   Applied at the first alarm on the five surge days, the other queue moves from 1.00× to
   1.28×, 1.14×, 1.18×, 1.03× and 1.09×. That is closer to what actually happened on every day.
3. **Make the log say which shape was used.** Today the handle-time assumption always reads "hold 60
   min, then fade over 60 min", even when the closed-ticket fade is in force.

**Why the fix is not in the numbers above:**
- The headline numbers describe the system as it was frozen and measured.
- The fix was found by studying these same days, the held-out WTH-H1 included, so re-measuring on them
  would flatter it.
- A fair check needs fresh surge days the fix has never seen.

**Cost of applying it later:** the fix changes only decisions made under an unexplained cause. Every other
decision replays from saved answers at no cost, and the replay shows exactly which days changed. The demo
day (OUT-01) is not affected. The estimated cost is about US$4–5 to re-run the five surge days, plus about
US$5–7 for a fresh set of surge days to confirm the gain.
