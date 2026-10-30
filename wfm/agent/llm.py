"""Claude client with a record/replay cache and cost tracking.

Modes (env WFM_LLM_MODE, or the constructor):
  record  - replay a saved response when the exact request was seen before, else call Claude and save it (default)
  cached  - replay only; never call the API (safe demo mode, free test runs)
  live    - always call Claude and refresh the saved response

Requests opt into server-side refusal fallbacks ("default" routing), so a rare safety
decline is retried on Anthropic's recommended fallback model instead of stalling the loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field

import anthropic
from anthropic.types.beta import BetaMessage

from wfm.config import DATA_DIR, load_env

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CACHE_DIR = DATA_DIR / "llm_cache"
# USD per million tokens for claude-opus-5-5 (input, output, cache read, cache write).
PRICE = {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00}


class LLMUnavailable(RuntimeError):
    pass


def _jsonable(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    raise TypeError(f"cannot serialise {type(obj)}")


def echo_content(message: BetaMessage) -> list:
    """Assistant content to append to the history. After a mid-output fallback, blocks the
    declined model produced before the last `fallback` marker must not be echoed back."""
    blocks = list(message.content)
    marks = [i for i, b in enumerate(blocks) if b.type == "fallback"]
    if not marks:
        return blocks
    cut = marks[-1]
    drop = {"thinking", "redacted_thinking", "tool_use"}
    return [b for i, b in enumerate(blocks) if i >= cut or b.type not in drop]


@dataclass
class Usage:
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    by_model: dict = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return round((self.input_tokens * PRICE["input"] + self.output_tokens * PRICE["output"]
                      + self.cache_read_tokens * PRICE["cache_read"]
                      + self.cache_write_tokens * PRICE["cache_write"]) / 1e6, 4)

    def add(self, msg: BetaMessage) -> None:
        u = msg.usage
        self.calls += 1
        self.input_tokens += u.input_tokens or 0
        self.output_tokens += u.output_tokens or 0
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.by_model[msg.model] = self.by_model.get(msg.model, 0) + 1


class LLM:
    def __init__(self, mode: str | None = None):
        load_env()
        self.mode = mode or os.environ.get("WFM_LLM_MODE", "record")
        if self.mode not in ("record", "cached", "live"):
            raise ValueError(f"unknown LLM mode {self.mode!r}")
        self.usage = Usage()  # live calls made by this instance (real spend)
        self.replayed = Usage()  # answers served from the cache: free now, but this is what they cost to make
        self.live_seconds = 0.0
        self.replayed_seconds = 0.0  # original latency of answers served from the cache
        self.untimed_replays = 0  # cached answers saved before latency was recorded
        self._client: anthropic.Anthropic | None = None

    @property
    def seconds(self) -> float:
        """Claude time behind the answers given so far (live, or as originally recorded)."""
        return self.live_seconds + self.replayed_seconds

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(max_retries=6)  # ride out brief rate limits in long runs
        return self._client

    def create(self, **params) -> BetaMessage:
        payload = json.dumps({"model": MODEL, **params}, sort_keys=True, default=_jsonable)
        key = hashlib.sha256(payload.encode()).hexdigest()
        path = CACHE_DIR / key[:2] / f"{key}.json"
        if self.mode != "live" and path.exists():
            data = json.loads(path.read_text())
            msg = BetaMessage.model_validate(data["message"] if "message" in data else data)
            self.usage.cache_hits += 1
            self.replayed.add(msg)
            if "elapsed_s" in data:
                self.replayed_seconds += data["elapsed_s"]
            else:
                self.untimed_replays += 1
            return msg
        if self.mode == "cached":
            raise LLMUnavailable(f"no saved response for request {key[:12]} (cached mode)")
        t0 = time.perf_counter()
        try:
            msg = self.client.beta.messages.create(model=MODEL, betas=[FALLBACK_BETA], fallbacks="default", **params)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.RateLimitError,
                anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            raise LLMUnavailable(f"Claude unavailable ({exc.__class__.__name__})") from exc
        except anthropic.BadRequestError as exc:
            if "credit balance" in str(exc):
                raise LLMUnavailable("Claude unavailable (Anthropic account credit exhausted)") from exc
            raise
        elapsed = time.perf_counter() - t0
        self.usage.add(msg)
        self.live_seconds += elapsed
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"message": msg.model_dump(mode="json"), "elapsed_s": round(elapsed, 2)}))
        return msg
