# One-command entry points. `make all` = install + build data + run checks.
.PHONY: all setup data test clean check-key detector diagnose agent-days demo eval report offline-check rehearse video package

# macOS may flag the venv's .pth files as hidden, which Python skips; point Python at the repo directly.
export PYTHONPATH := $(CURDIR)

all: setup data test

setup:
	uv sync

data:
	uv run python -m wfm.data.history
	uv run python -m wfm.data.scenarios
	uv run python -m wfm.forecast.sarima
	uv run python -m wfm.staffing.roster
	uv run python scripts/calibrate.py
	uv run python scripts/phase1_overview.py
	uv run python scripts/phase2_replay.py

test:
	uv run pytest -q

# Detector false-alarm / delay check across calm and bad days (no AI calls).
detector:
	uv run python scripts/calibrate_detector.py

# Agent diagnosis on all 31 days. Calls Claude for any request not already saved
# (about $1.50 the first time); WFM_LLM_MODE=cached replays saved answers for free.
diagnose:
	uv run python scripts/phase3_diagnose.py

# Full agent days (plan, approve, execute, monitor) on 5 bad days + the reject-overtime demo.
# About US$2-3 of Claude calls the first time; free when replayed.
agent-days:
	uv run python scripts/phase4_run.py

# The split-screen demo dashboard (opens in your browser). Offline mode needs no internet.
demo:
	uv run streamlit run app/dashboard.py

# Four-way evaluation (resumable; spend capped). ~US$20 of Claude calls the first time; free afterwards.
eval:
	uv run python scripts/run_eval.py --approaches none playbook
	uv run python scripts/run_eval.py --approaches plain_llm --cap-usd 8
	uv run python scripts/run_eval.py --approaches agent --cap-usd 28
	uv run python scripts/judge_explanations.py
	uv run python scripts/replay_audit.py

# Rebuild docs/eval_report.md and its chart from saved results (free).
report:
	uv run python scripts/eval_report.py

# Pitch safety net: no API key, no connection; the pitch path and every picker day must finish (free).
offline-check:
	uv run python scripts/offline_check.py

# Three timed rehearsals of the pitch path in Chrome (free; needs Google Chrome installed).
rehearse:
	uv run --with playwright python scripts/rehearse.py --runs 3

# Re-record the captioned backup video to media/demo_backup.mp4 (free).
video:
	uv run --with playwright python -m playwright install ffmpeg
	uv run --with playwright --with imageio-ffmpeg python scripts/rehearse.py --video

# Submission zip in dist/: code, data, saved Claude answers, docs and video; never .env or .venv.
package:
	mkdir -p dist
	rm -f dist/wfm-rescue-agent.zip
	zip -qr dist/wfm-rescue-agent.zip . -x '.env' '.venv/*' 'dist/*' 'runs/*' '*/__pycache__/*' '.pytest_cache/*' '*.DS_Store' 'media/_recording/*'
	@echo "dist/wfm-rescue-agent.zip: $$(du -h dist/wfm-rescue-agent.zip | cut -f1)"

clean:
	rm -rf .pytest_cache **/__pycache__

# Says whether the Anthropic API key is set (in the shell or .env) without printing it.
check-key:
	@uv run python -c "import os; from wfm.config import load_env; load_env(); k = os.environ.get('ANTHROPIC_API_KEY', ''); print('ANTHROPIC_API_KEY is set (' + k[:7] + '...)' if k else 'ANTHROPIC_API_KEY is NOT set: paste it into .env')"
