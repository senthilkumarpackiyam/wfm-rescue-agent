"""Phase 7: rehearse the pitch demo in a real browser, time each step, and record the backup video.

    uv run --with playwright python scripts/rehearse.py --runs 3
    uv run --with playwright --with imageio-ffmpeg python scripts/rehearse.py --video [--voice Aman | --silent]

Starts its own dashboard on a spare port with saved answers only, then drives the installed Chrome
through the pitch path: play into the 10:20 outage, reject "no overtime budget today", approve the
re-plan, run to the end approving any later plans. Fails if the page ever shows an error.
With --video the same path is recorded with captions and a spoken narration (the Mac's built-in
text-to-speech, so it works offline and costs nothing) to media/demo_backup.mp4.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "media"
SIZE = {"width": 1920, "height": 1080}
WAITING = "Recommendation waiting for you"

# What the narrator says at each caption (spelled for text-to-speech: "U P I", "ten twenty").
NARRATION = {
    "open": "This is a bank contact centre on an ordinary Tuesday morning. The bars are calls arriving, "
            "and the grey line is the morning forecast. Service level is healthy.",
    "sides": "On the left, the morning plan runs unchanged. On the right, the same day, with exactly the "
             "same callers, but with the agent watching.",
    "play": "We press play. One simulated minute passes every second.",
    "outage": "At ten twenty, the U P I payment switch fails. Payment calls surge on both sides.",
    "alert": "At ten forty-five, the agent raises an alert. Watch the trace: it diagnoses the cause, citing the "
             "incident ticket and the jump in payment calls, re-forecasts the rest of the day, and tests several "
             "plans in a simulator.",
    "trace": "Every step stays in the trace, newest first. The simulator predicted each option's service level, "
             "and every plan passed the policy check.",
    "options": "Here are the options, compared on predicted service level, hang-ups, extra cost, and policy. "
               "The agent recommends one, but nothing changes until the supervisor decides.",
    "reject": "The supervisor rejects it. There is no overtime budget today.",
    "replan": "The agent turns that sentence into a rule, no overtime, and plans again.",
    "newplan": "The new plan uses no overtime. The agent says honestly that it will still miss the eighty "
               "percent target, and asks for the duty manager. The supervisor approves.",
    "finish": "Approved plans now change the real day. We run to the end of the day, approving the agent's "
              "later plans as they come.",
    "compare": "Same callers, same random seed. Since the outage, payments service level was forty-one percent "
               "without the agent, and sixty-five percent with it. Five hundred and sixty-eight fewer callers "
               "hung up.",
    "result": "Across both queues, from the first alert to close, service level rose from fifty-eight to "
              "seventy-seven percent, for two thousand, seven hundred and sixty-three rupees, with no overtime.",
}

CAPTION_JS = """text => {
  let el = document.getElementById('rehearse-caption');
  if (!el) {
    el = document.createElement('div');
    el.id = 'rehearse-caption';
    el.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:999999;' +
      'max-width:1500px;padding:14px 26px;border-radius:10px;background:rgba(11,11,11,.86);color:#fcfcfb;' +
      'font:600 26px/1.35 system-ui,sans-serif;text-align:center;box-shadow:0 4px 18px rgba(0,0,0,.25)';
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.style.display = text ? 'block' : 'none';
}"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def start_dashboard(port: int) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app/dashboard.py", "--server.port", str(port),
                             "--server.headless", "true"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            if urllib.request.urlopen(f"http://localhost:{port}/_stcore/health", timeout=1).status == 200:
                return proc
        except OSError:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("dashboard did not start")


def speak_all(voice: str, out_dir: Path) -> dict[str, tuple[Path, float]]:
    """One WAV per narration line, with its length in seconds."""
    clips = {}
    for key, text in NARRATION.items():
        path = out_dir / f"{key}.wav"
        subprocess.run(["say", "-v", voice, "-o", str(path), "--file-format=WAVE", "--data-format=LEI16@22050",
                        text], check=True)
        with wave.open(str(path)) as w:
            clips[key] = (path, w.getnframes() / w.getframerate())
    return clips


class Demo:
    def __init__(self, page: Page, video: bool, clips: dict[str, tuple[Path, float]] | None = None):
        self.page, self.video, self.clips = page, video, clips or {}
        self.marks: list[tuple[str, float]] = []
        self.t0 = time.perf_counter()
        self.approvals = 0
        self.video_start = time.perf_counter()
        self.spoken: list[tuple[float, Path]] = []  # (seconds into the video, clip)
        self.busy_until = 0.0

    def body(self) -> str:
        return self.page.inner_text("body")

    def wait_for(self, js: str, timeout_s: float = 180) -> None:
        self.page.wait_for_function(f"() => {{ const b = document.body.innerText; return {js}; }}",
                                    timeout=timeout_s * 1000)

    def mark(self, step: str) -> None:
        now = time.perf_counter()
        self.marks.append((step, now - self.t0))
        self.t0 = now

    def caption(self, text: str, hold_s: float = 0, say: str | None = None) -> None:
        if not self.video:
            return
        if (left := self.busy_until - time.perf_counter()) > 0:  # let the narrator finish the last line
            self.page.wait_for_timeout(left * 1000)
        self.page.evaluate(CAPTION_JS, text)
        if say in self.clips:
            path, seconds = self.clips[say]
            now = time.perf_counter()
            self.spoken.append((now - self.video_start, path))
            self.busy_until = now + seconds + 0.4
            hold_s = max(hold_s, seconds + 0.4) if hold_s else 0
        self.page.wait_for_timeout(hold_s * 1000)

    def scroll_to(self, text: str | None) -> None:
        if not self.video:
            return
        if text is None:
            self.page.evaluate("() => document.querySelector('[data-testid=stMain]').scrollTo({top: 0, behavior: 'smooth'})")
        else:  # to the top of the screen, even when the heading is already in view and its content is not
            self.page.get_by_text(text, exact=False).first.evaluate(
                "e => e.scrollIntoView({block: 'start', behavior: 'smooth'})")
        self.page.wait_for_timeout(800)

    def click(self, name: str) -> None:
        self.page.get_by_role("button", name=name).first.click(timeout=30000)

    def approve(self) -> None:
        self.click("Approve")
        self.approvals += 1
        self.wait_for(f"!b.includes('{WAITING}') || b.includes('Day complete')")
        self.page.wait_for_timeout(1500)


def run_pitch(demo: Demo, url: str) -> None:
    pg = demo.page
    pg.goto(url)
    demo.wait_for("b.includes('Agent trace') && b.includes('10:00')", 90)
    pg.wait_for_timeout(2500)
    demo.mark("open the dashboard")
    demo.caption("A normal Tuesday at 10:00: calls (bars) follow the morning forecast (grey line); "
                 "service level is healthy", 7, say="open")
    demo.caption("Left: the morning plan runs unchanged. Right: the same day, same callers, with the agent", 6,
                 say="sides")

    demo.caption("Press Play: one simulated minute per second", say="play")
    demo.click("Play")
    if demo.video:
        pg.wait_for_timeout(21000)
        demo.caption("10:20: the UPI payment switch fails. Payment calls surge on both sides", say="outage")
        demo.wait_for("b.includes('Detect')")  # 10:45: the agent starts; show its steps as they stream in
        demo.scroll_to("Agent trace")
        demo.caption("10:45: the agent detects, diagnoses with evidence, re-forecasts, tests options and checks policy",
                     say="alert")
    demo.wait_for(f"b.includes('{WAITING}')")
    demo.mark("play into the outage until the agent recommends")
    demo.scroll_to("Agent trace")
    demo.caption("Every step is logged: the simulated service level of each option, then the policy check", 9,
                 say="trace")
    demo.scroll_to(WAITING)
    demo.caption("Options compared on predicted service level, hang-ups, extra cost and policy", 9, say="options")

    demo.caption("The supervisor rejects it: no overtime budget today", say="reject")
    box = pg.get_by_label("Reason, if you reject")
    box.click()
    box.press_sequentially("no overtime budget today", delay=60 if demo.video else 0)
    demo.click("Reject and re-plan")
    demo.wait_for(f"b.includes('New limits for today') && b.includes('{WAITING}') "
                  "&& !b.includes('Re-planning under your instruction')")
    demo.mark("reject and wait for the re-plan")
    demo.scroll_to("Agent trace")
    demo.caption("The agent turns the sentence into a rule (no overtime) and re-plans under it", 7, say="replan")
    demo.scroll_to(WAITING)
    demo.caption("New plan without overtime; it says honestly when the target is out of reach", 7, say="newplan")
    demo.approve()
    demo.mark("approve")

    demo.caption("Approved plans touch the real day. Run to the end, approving any later plans", say="finish")
    demo.scroll_to(None)
    for _ in range(15):
        body = demo.body()
        if "Day complete" in body:
            break
        if WAITING in body:
            demo.approve()
            continue
        demo.click("Run to end")
        demo.wait_for(f"b.includes('{WAITING}') || b.includes('Day complete')")
    demo.mark("run to the end")
    demo.scroll_to(None)
    demo.caption("Same callers, same seed: without the agent vs with the agent", 8, say="compare")
    demo.scroll_to("Day complete")
    line = next((x for x in demo.body().splitlines() if x.startswith("From the first alert")), "")
    demo.caption(line, 9, say="result")
    demo.caption("")


def check_page(demo: Demo) -> list[str]:
    problems = []
    if demo.page.locator("[data-testid=stException]").count():
        problems.append("the page shows a Python error")
    body = demo.body()
    if "AI assistant unavailable" in body:
        problems.append("a saved answer was missing (AI assistant unavailable)")
    if "Day complete" not in body:
        problems.append("the day did not finish")
    return problems


def to_mp4(webm: Path, mp4: Path, spoken: list[tuple[float, Path]]) -> None:
    """Encode the recording as H.264 and lay each narration clip in at the moment its caption appeared."""
    import imageio_ffmpeg

    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(webm)]
    for _, clip in spoken:
        cmd += ["-i", str(clip)]
    if spoken:
        delays = ";".join(f"[{i}:a]adelay={round(t * 1000)}:all=1[a{i}]" for i, (t, _) in enumerate(spoken, 1))
        mix = "".join(f"[a{i}]" for i in range(1, len(spoken) + 1))
        cmd += ["-filter_complex", f"{delays};{mix}amix=inputs={len(spoken)}:normalize=0[aout]",
                "-map", "0:v", "-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-movflags", "+faststart", str(mp4)]
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--video", action="store_true", help="record one captioned, narrated run to media/")
    ap.add_argument("--voice", default="Aman", help="macOS voice for the narration (try: say -v '?')")
    ap.add_argument("--silent", action="store_true", help="record the video without narration")
    args = ap.parse_args()
    port = free_port()
    url = f"http://localhost:{port}/?scenario=OUT-01&start=10:00&mode=cached"
    voice_dir = Path(tempfile.mkdtemp(prefix="narration-"))
    clips = speak_all(args.voice, voice_dir) if args.video and not args.silent else {}
    server = start_dashboard(port)
    failed = False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            for run in range(1, (1 if args.video else args.runs) + 1):
                tmp = MEDIA / "_recording"
                ctx = browser.new_context(viewport=SIZE, color_scheme="light",
                                          **({"record_video_dir": str(tmp), "record_video_size": SIZE}
                                             if args.video else {}))
                page = ctx.new_page()
                demo = Demo(page, args.video, clips)
                start = time.perf_counter()
                run_pitch(demo, url)
                problems = check_page(demo)
                total = time.perf_counter() - start
                ctx.close()
                print(f"\nrehearsal {run}: {'OK' if not problems else 'PROBLEMS: ' + '; '.join(problems)}")
                for step, secs in demo.marks:
                    print(f"  {step:48} {secs:6.1f} s")
                print(f"  {'total (machine time, no talking)':48} {total:6.1f} s; later plans approved: "
                      f"{demo.approvals - 1}")
                failed |= bool(problems)
                if args.video:
                    webm = next(tmp.glob("*.webm"))
                    MEDIA.mkdir(exist_ok=True)
                    to_mp4(webm, MEDIA / "demo_backup.mp4", demo.spoken)
                    shutil.rmtree(tmp)
                    print(f"  video: {(MEDIA / 'demo_backup.mp4').relative_to(ROOT)}"
                          + (f", narrated by {args.voice} ({len(demo.spoken)} lines)" if demo.spoken else ", silent"))
            browser.close()
    finally:
        server.terminate()
        shutil.rmtree(voice_dir, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
