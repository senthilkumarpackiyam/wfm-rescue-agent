# Phase 2, explained in plain language

## The big idea of Phase 2

In Phase 1 we *wrote down* 30 bad days. In Phase 2 we built a **flight simulator for a
call centre**, so those days actually *happen*, caller by caller, and we can try fixes
safely and see what they would really do.

## What we built, with analogies

### 1. The forecaster (SARIMA): "how many calls tomorrow?"
Like a weather forecaster who studies past seasons, it learns the weekly rhythm from 21
months of history (busy Mondays, quiet Sundays, slow growth) and predicts each day's total.
It then spreads that total across the day using the shape of the last 8 same weekdays.

**How good is it?** We hid September, trained on everything before it, and predicted
September. Daily totals were off by about **3% (payments)** and **4.5% (general)**. That is
about as good as it can get, because some daily randomness is genuinely unpredictable.

### 2. Erlang A: "how many people do I need?"
This is the standard call-centre formula. You give it "100 calls in the next 15 minutes,
5 minutes each, callers hang up after about 3 minutes of waiting", and it answers "you need
about 36 agents on the phones to answer 80% within 20 seconds". We checked it against the
textbook formula and it matches to four decimal places.

### 3. The morning staffing plan (the roster)
Turns "36 needed at 11:00, 20 needed at 08:00..." into real people with real shifts:
8.5-hour full-timers, 4.5-hour part-timers, two tea breaks and a lunch each (staggered so
not everyone leaves at once), coaching parked in quiet periods, and a quarter of staff
trained on both phone lines. About **63 payments and 60 general agents** per day.

### 4. The simulator: the heart of Phase 2
Think of a **very fast, very honest replay of a whole working day**:
- Each of ~6,500 callers rings at a specific second, with a reason ("UPI payment failed").
- Free agents pick up the caller who has waited longest.
- Impatient callers hang up if they wait too long.
- Agents go on break, lunch and coaching exactly as the roster says; sick agents never appear.

A whole day replays in **0.07 seconds** (the plan's target was under 2 minutes).

**The key trick: same callers, different decisions.** All callers are decided in advance by
the day's seed. To test "what if we offer overtime at 11:00?", we replay the day with that
change. Everything before 11:00 is identical (we test this automatically), so any
difference afterwards is caused only by the decision. That is how the agent will compare
options fairly in Phase 4.

### 5. The fixes actually work in the simulator
Each lever on the menu now has a real, physical effect:
- **Overtime** adds extra people, but only 30 minutes after the offer, and only ~40% accept.
- **Moving cross-skilled agents** helps one line but measurably hurts the other.
- **IVR outage message** ("we know UPI is down") stops 20% of the *matching* callers only.
- **Chat** can only take simple questions (balance, status), never card declines.
- **Mandatory compliance training** can never be cancelled.

## Two things we learned and fixed (honest notes)

1. **Callers were too impatient.** At 90 seconds of patience, 8% of callers hung up even on
   a good day. Banks typically see 2-4%, so we raised patience to 3-4 minutes.
2. **The formula and the simulator disagreed, which the plan predicted.** Erlang A looks at
   each phone line alone, but in the simulator the cross-trained staff quietly float to
   whichever line is busy. That pooling is worth a lot: planning to the textbook 80% gave
   normal days of 97%, meaning the centre was overstaffed. As the plan says, *trust the
   simulator*: we tested six planning levels on 31 normal days and chose the one where an
   ordinary day averages **91% and never drops below 80%**.

## Calibration: is every bad day really bad, and can it be saved?
For each of the 30 days we compared three versions of the same day (`docs/calibration.md`):

| | Average service level |
|---|---|
| Same day without the disruption | 91.5% |
| Disruption, nobody does anything | **58%** (all 30 days fail the 80% promise) |
| Disruption, strong human response 30 minutes in | **77.5%** (25 of 30 lifted by 10+ points) |

So the test is fair: doing nothing clearly fails, good decisions clearly help, and a few
days are so hard that even a strong response struggles. Those hard days are where we will
look for the "honest failure case" the plan asks for.

## How to see it yourself
- Open `docs/img/phase2_replay.png`:
  - **A**: the outage calls against the forecast.
  - **B**: the service level crashing to near 0% and the rescue climbing back.
  - **C**: hang-ups falling from 913 to 314.
  - **D**: all 30 days by type.
- Open `docs/calibration.md` for every day's numbers.
- Run `make all` (about 30 seconds) and you should see `34 passed`.
