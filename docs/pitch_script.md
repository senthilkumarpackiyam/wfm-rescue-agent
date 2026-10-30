# Pitch script: 6 minutes

The slide deck lives at the private link in PROGRESS.md and has these notes as speaker notes. Timings follow
the project plan's demo-day script. "Machine time" was measured by `make rehearse` (three runs,
offline answers): the dashboard work itself takes about 1 min 50 s. The rest of the demo slot is
for talking.

## Before you go on stage (5 minutes)

1. Open the dashboard with `make demo`, then open this bookmark:
   `http://localhost:8501/?scenario=OUT-01&start=10:00&mode=cached`
   - The page loads at 10:00 on the outage day.
   - **AI answers** is set to **Offline: saved answers only**.
   - Speed is **1**, the pause is **1.0**, the charts show **payments**.
2. Collapse nothing and zoom nothing: the charts fill the top half of a 1080p screen, and the
   trace and the decision card are one scroll down.
3. Have `media/demo_backup.mp4` open in a second window, paused at 0:00.
4. Have the slides open on slide 1.
5. Optional safety check the night before: run `make offline-check`. It takes about 2 minutes and
   proves the demo needs no key and no internet.

## The six minutes

| Clock | Screen | Do | Say (the gist) |
|---|---|---|---|
| 0:00 | Slide 1 | — | "Every contact centre plans its day at 8 a.m. By 11, that plan is wrong, and an analyst has minutes to fix it." |
| 0:05 | Slide 2 | — | The trigger, the cost, today's fix. "That is a reasoning job, which is why it suits an agent." |
| 0:40 | Slide 3 (optional) | — | "Code does the maths, Claude does the reasoning, and a person approves every action." Skip if you are behind. |
| 0:45 | Slide 4, then the dashboard | Click **▶ Play** | "Left is today's plan, right is the agent: the same callers and the same seed." At 10:20 the UPI switch fails and both sides spike. |
| ≈1:35 | Dashboard | Nothing to click: the clock stops by itself at 10:45 and the trace streams for about 10 s | Scroll down. Point at **Diagnose** (it cites the ticket and the failed-payment call reasons), **Re-forecast**, **Simulate** (the predicted service level for each option) and **Check policy**. Then the options table: service level, hang-ups, extra cost, policy OK. |
| 3:00 | Dashboard | Type `no overtime budget today` in the reason box, then click **❌ Reject and re-plan** (about 12 s) | "The supervisor says no." Point at "New limits for today: no overtime". The new plan has no overtime, says honestly that it will miss 80%, and calls in the duty manager. |
| ≈3:20 | Dashboard | Click **✅ Approve** | "Only approved plans touch the real day." |
| 3:45 | Dashboard | Scroll up, click **⏩ Run to end**, and approve each later card (3 of them, about 37 s in all) | "Same callers, same seed": 41% against 65% service level since the outage, and 907 against 339 callers who hung up, for ₹2,763 with no overtime. |
| 4:30 | Slide 6 | — | 30 bad days and four approaches: highest service level, lowest cost per point, 0 rule breaks, 30/30 causes. It scored the same on 6 unseen days, and nobody obeyed the planted instructions. |
| 5:05 | Slide 7 | — | "Here is where it loses": rain and festival days. Two bugs in our forecast code, not in Claude. The fix is designed and deliberately not applied. |
| 5:30 | Slide 8 | — | "This sits as an agent layer on top of any WFM platform." Thank the panel. |

Slide 5 (One outage, two endings) is a recap. Use it if you have 10 seconds spare, or instead of
the live demo if the demo cannot run.

## If something goes wrong

| Problem | What to do |
|---|---|
| The dashboard will not start or freezes | Switch to `media/demo_backup.mp4` and talk over it. It has captions and runs 3 min 6 s, so skip ahead if short of time. |
| No internet or no API key | Nothing to do: offline mode never calls Claude. |
| A red "AI assistant unavailable" banner | You are not in offline mode, or you typed a different rejection. Reload the bookmark and type the reason exactly as written. |
| The panel picks another disruption ("crowd moment") | Choose it in the sidebar, keep offline mode, click **Load disruption**, then **⏭ Next agent step**, and approve each card. Every day in the list replays offline. Only a *different* rejection text needs live Claude, through the "Saved answers, live if missing" mode, which takes about a minute and costs about US$0.20–0.50. |

## Panel questions

The answers are on appendix slides 9–12: questions we expect, full results, under the hood, and
what it costs to run.
