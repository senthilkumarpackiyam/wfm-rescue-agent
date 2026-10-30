# Phase 7, explained in plain language

## The big idea of Phase 7

Phases 1–6 built and proved the agent. Phase 7 makes sure **the six minutes on stage cannot go
wrong**. It is like a wedding planner's last week: the speech written, the venue tested with the
power off, a rehearsal, and a recording in case the band does not turn up.

## What we prepared

| Item | What it is | Where |
|---|---|---|
| **Slide deck** | 8 slides for the talk plus 4 for questions, with speaker notes and timings | The deck link (private until you share it) |
| **Presenter script** | Second-by-second: what is on screen, what to click, what to say, and what to do if something breaks | `docs/pitch_script.md` |
| **Offline proof** | The demo was run with the API key removed and any connection to Claude blocked. Every disruption in the dropdown still finished. | `make offline-check` |
| **Rehearsals ×3** | A robot clicked through the exact pitch path in Chrome three times and timed every step | `make rehearse` |
| **Backup video** | A 3-minute recording of the demo with captions and a spoken narration, playable anywhere | `media/demo_backup.mp4` |
| **Submission zip** | Code, data, saved Claude answers, documents and the video, but never your API key | `make package` → `dist/` |

## How the six minutes are spent

The project plan's script is mostly *live demo*. The slides only frame it:

| Time | What happens |
|---|---|
| 0:00–0:45 | Slides: the problem, and why it is a reasoning job |
| 0:45–4:30 | The dashboard: the outage hits, the agent recommends, you reject overtime, it re-plans, the day finishes |
| 4:30–5:30 | Slides: 30 bad days and four approaches, then the honest failure case |
| 5:30–6:00 | Close: "an agent layer on top of any WFM platform" |

The dashboard's own work takes about **1 minute 50 seconds**: about 56 s to play into the outage
until the recommendation, 12 s to re-plan, and 37 s to finish the day. That leaves about 2 minutes
of the demo slot for you to explain what the audience is seeing.

## Two things the rehearsals caught

Robots rehearsing in a real browser found two problems a person might only notice on stage:

1. **The agent's trace blinked** about a quarter of a second in every second. The page was
   emptying the trace box before redrawing the charts. Now it refills at once.
2. **The two sides' charts did not line up** at the end, because the green "+24 pts" badges pushed
   the right side down. Both sides now reserve the same space.

Both are fixed, and the video was re-recorded after the fixes.

## The safety net, in order

1. **Offline mode** (the default for the pitch): no internet and no API key needed. Every
   disruption in the dropdown works, so a panel member can pick one live.
2. **The backup video**: if the laptop, the projector or the dashboard fails, play it. It
   narrates itself; mute it if you would rather talk over it.
3. **Slide 5** shows the demo's results as a slide, if even the video cannot play.

## What it cost

Phase 7 used **no Claude calls at all**: everything ran from saved answers. The project total
stays at **US$36.70**.

## Your to-do list before submitting

1. **Fill in the placeholders** on slides 1 and 8: team names and the date.
2. **Share the deck**: it is private. Use the Share menu on the deck page, or download it as
   PowerPoint or PDF from that page.
3. **Rehearse it yourself** at least once with `docs/pitch_script.md`, out loud and with a timer.
   The robot proved the clicks work; only you can check the talking fits.
4. **Optional, but it makes a good slide:** have the team grade the 20 recommendations in
   `docs/eval/label_sheet.csv`. Then I'll run the agreement check and add the result to the
   results slide.
5. **Submit**: run `make package` and send `dist/wfm-rescue-agent.zip`, the deck and the video.
