# Phase 1, explained in plain language

## The big idea of Phase 1

Before anyone builds the rescue agent, we need two things:

1. **A practice world**: a fake contact centre with a believable past, so we can test
   without touching a real one.
2. **A set of exam questions with sealed answers**: 30 bad days where *we* know what
   really went wrong, so we can later mark the agent's work objectively.

Phase 1 builds both, plus the "plug sockets" that let every later piece connect.

## What we built, with analogies

### 1. The rulebook of our fictional contact centre (`config/contact_center.yaml`)
Think of it as the **fact sheet of a pretend bank's call centre**: open 8 am to 8 pm, two
phone lines ("Payments & UPI" and "Accounts & General"), about 5,500 calls a day, and the
promise that **80% of callers are answered within 20 seconds** (the *service level*).
Every later component reads this one sheet, so nobody works from different assumptions.

### 2. The menu of fixes (`config/levers.yaml`)
Like a **restaurant menu with prices, cooking times and "max 2 per table" notes**. Each
fix (delay breaks, cancel coaching, overtime, move cross-trained staff, IVR message or
callback, push to chat) has:
- a **cost** (overtime is Rs 360/hour; delaying a break is free),
- a **delay** (overtime takes about 30 minutes because people must accept and log in),
- a **hard limit** (a break can slip at most 45 minutes; overtime budget is Rs 15,000 a day).
These limits are enforced by plain code later, never by the AI's judgement.

### 3. The plug sockets (`wfm/schemas.py`)
Four people (or four workstreams) will build the simulator, forecaster, agent and dashboard
in parallel. If each invents their own way to describe "a 15-minute slot" or "an overtime
offer", nothing fits together at the end. So on Day 1 we **agree the exact shape of every
piece of information** that passes between them, like agreeing the shape of plug sockets
before four electricians wire different rooms. The computer rejects anything badly shaped
(for example, a time of "25:00" or a lever called "fire_everyone").

### 4. A fake but realistic past (`data/history/intervals.csv`)
The forecaster learns from history, the way you would guess tomorrow's traffic from the
last year of traffic. We generated **21 months of call records in 15-minute slices**
(61,248 rows). They include the patterns a real centre has:
- busy Mondays and quiet Sundays (the zig-zag in chart A),
- two busy humps each day, late morning and mid-afternoon (chart B),
- slow growth over time plus a gentle yearly wave,
- everyday randomness that no forecast can predict.

### 5. The hidden answer key (`wfm/data/demand_model.py`)
The fake history comes from a **secret recipe** for how many calls *really* arrive. The
same recipe will produce the real calls on each bad day. The forecaster is never allowed
to look at the recipe, only at the history it produced, exactly like a real forecaster
who only sees past data.

### 6. The 30 bad days (`data/scenarios/scenarios.json`, readable table in `docs/scenarios.md`)
These are **fire drills with a sealed envelope saying what really caused the fire**.

| Kind | Count | What happens | What it tests |
|---|---|---|---|
| Outage | 8 | A payment system breaks and calls double for 1-3 hours | Can the agent find the incident ticket and the matching call reasons? |
| Marketing | 6 | A campaign email or TV ad drives calls for 5-6 hours | Does it check the marketing calendar? |
| Weather / seasonal | 4 | Calls up 25-45% all day, and nothing in any feed explains it | Does it honestly say "cause unknown" instead of inventing a reason? |
| Sick-call wave | 6 | 18-28% of agents don't show up or go home ill | Does it see that the problem is fewer staff, not more calls? |
| Slow systems | 4 | Same number of calls, but each takes 30-55% longer | Does it spot that the work got longer, not bigger? |
| Adversarial | 2 | A real outage, but the ticket hides a note telling the AI to break the rules | Does it ignore and report the trick? |

Each day also has **red herrings**, like a mystery novel: unrelated tickets ("HR portal
slow") and harmless marketing posts, so the agent can't just grab the first clue it sees.
Some clues are deliberately **late or vague** (a ticket opened 40 minutes after calls
started rising), because that is what real mornings look like.

Day **OUT-01** is our scripted demo: a UPI outage at 10:20 on Tuesday 6 October 2026
(chart C shows calls more than doubling against the plan).

### 7. Fixed "seeds"
Every bad day has a seed number, which works like **shuffling a deck of cards the same way
every time**. "No action", the rule-based playbook, the plain AI and our full agent all
face exactly the same callers at exactly the same moments, so the comparison is fair.

### 8. Automatic checks (`tests/`)
18 checks run in under a second, for example: "are there exactly 8 outage days?", "does
every clue the answer key cites actually exist in that day's feeds?", "is Monday busier
than Sunday?", "does regenerating the data give the identical result?".

## How to see it yourself

- Open `docs/img/phase1_overview.png` (the four charts).
- Open `docs/scenarios.md` (the 30 bad days in one table).
- In a terminal in the project folder, run `make all`. You should see `18 passed`.
