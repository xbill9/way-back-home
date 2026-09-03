"""Which Live model this process talks to, and what that model changes.

Everything that depends on *which* Live model is in use lives here, so main.py
and agent.py can ask a question instead of pattern-matching model names in
three places.

The two EAP models (invite: "Gemini API Early Access Program | Walkie-Talkie
and Clever-Chatter") are Gemini 3.x Live models whose ids do not say so:

    models/walkie-talkie    latency-optimised, no background thinking
    models/clever-chatter   background reasoning via thinking_config

That naming is the whole problem this module solves. ADK 2.6.3 decides how to
speak the Live protocol from the model *name* -- `_is_gemini_3_x_live()` means
"starts with `gemini-3.` and contains `-live`" -- and `LLMRegistry` only
resolves ids matching `gemini-*`. Both assumptions miss these two ids. See
`build_live_model()` and `EapLiveGemini`.

Access is per-GCP-project: the API key has to come from a project allowlisted
for the EAP, or the connection is refused.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import AsyncGenerator

from google.adk.models.base_llm_connection import BaseLlmConnection
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.genai import types

# The documented EAP endpoints. The `models/` prefix is part of the id.
WALKIE_TALKIE = "models/walkie-talkie"
CLEVER_CHATTER = "models/clever-chatter"
_EAP_MODELS = frozenset({WALKIE_TALKIE, CLEVER_CHATTER})

# walkie-talkie is the drop-in replacement for gemini-3.1-flash-live-preview:
# same latency-optimised architecture, plus async function calling and
# send_client_content for the whole session. clever-chatter trades latency for
# background reasoning, which this demo does not want -- it counts fingers.
DEFAULT_MODEL_ID = WALKIE_TALKIE

# `adk run` drives generateContent, which every Live-only model 404s on.
CLI_FALLBACK_MODEL = "gemini-2.5-flash"

# Only consulted when MODEL_ID selects clever-chatter. MINIMAL keeps the
# reasoning budget as close to walkie-talkie's latency as the model allows.
DEFAULT_THINKING_LEVEL = "MINIMAL"


def normalize_model_id(model_id: str) -> str:
    """Strip quotes/whitespace and accept the bare EAP names.

    `MODEL_ID="walkie-talkie"` is the mistake everyone makes once; the API
    wants `models/walkie-talkie`, so fix it here rather than 404 at connect
    time.
    """
    candidate = model_id.strip().strip('"').strip("'")
    if f"models/{candidate}" in _EAP_MODELS:
        return f"models/{candidate}"
    return candidate


def is_eap_model(model_id: str) -> bool:
    """Whether this is one of the two EAP Live models."""
    return normalize_model_id(model_id) in _EAP_MODELS


def supports_thinking(model_id: str) -> bool:
    """Whether the model accepts thinking_config. clever-chatter only.

    walkie-talkie rejects it, and gemini-3.1-flash-live-preview leaves
    thinkingLevel at `minimal` by default, which is what this app wants anyway.
    """
    return normalize_model_id(model_id) == CLEVER_CHATTER


def supports_blocking_function_calls(model_id: str) -> bool:
    """Whether synchronous (BLOCKING) function calls are still available.

    walkie-talkie keeps BLOCKING for backwards compatibility but defaults to
    NON_BLOCKING; clever-chatter hard-errors if BLOCKING is requested; and
    gemini-3.1-flash-live-preview is the mirror image -- BLOCKING only, with
    NON_BLOCKING unsupported. That last case is why the async-tool wiring in
    agent.py is conditional instead of unconditional.
    """
    return normalize_model_id(model_id) != CLEVER_CHATTER


def is_live_only(model_id: str) -> bool:
    """Whether the model speaks bidiGenerateContent and nothing else."""
    model_id = normalize_model_id(model_id)
    return model_id in _EAP_MODELS or "live" in model_id


def get_model_id() -> str:
    """The model id for this process.

    `MODEL_ID` wins when set. The exception is `adk run`, which uses
    generateContent: a Live-only model 404s there, so the CLI falls back to a
    model that speaks both. `make testadk` therefore exercises a different
    model than production -- set MODEL_ID to a non-Live model to pick your own.
    """
    is_adk_run = any("adk" in arg.lower() for arg in sys.argv) and "run" in sys.argv
    model_id = normalize_model_id(os.getenv("MODEL_ID", "")) or DEFAULT_MODEL_ID

    if is_adk_run and is_live_only(model_id):
        return CLI_FALLBACK_MODEL
    return model_id


class EapLiveGemini(Gemini):
    """`Gemini` with the Gemini 3.x Live wire behaviour forced on.

    ADK infers that behaviour from the model name, and `models/walkie-talkie`
    matches nothing, so without this every 3.x branch in `GeminiLlmConnection`
    silently takes the legacy path:

    * realtime media goes out as `send_realtime_input(media=...)` instead of
      the split `audio=` / `video=` fields the 3.x Live models expect;
    * tool calls are buffered until `turn_complete` instead of being yielded
      as they arrive. That one is fatal here: these models call tools
      asynchronously and `turnComplete` no longer means the model is idle, so
      a `report_digit` arriving after turn_complete would sit in the buffer
      until the *next* turn -- or forever;
    * input transcription is accumulated as partials rather than taken as the
      single final event 3.x Live sends.

    ADK offers no supported switch: the flag is computed in
    `GeminiLlmConnection.__init__` from the model name and never read from
    config. So the seam is one assignment on the connection ADK just built,
    which is as close to the caller as this can be fixed. It is a subclass and
    not a patch -- nothing in ADK is rebound, and non-EAP models never touch
    this class. Delete it once ADK recognises the EAP ids.
    """

    @contextlib.asynccontextmanager
    async def connect(
        self, llm_request: LlmRequest
    ) -> AsyncGenerator[BaseLlmConnection, None]:
        async with super().connect(llm_request) as connection:
            connection._is_gemini_3_x_live = True
            yield connection


def build_live_model(model_id: str) -> str | Gemini:
    """The value to hand to `Agent(model=...)`.

    Non-EAP ids stay strings, so ADK resolves them exactly as it did before.
    EAP ids have to be instances: `LLMRegistry` only matches `gemini-*`, so
    `Agent(model="models/walkie-talkie")` raises "Model models/walkie-talkie
    not found" at import time -- before anything reaches the network.
    """
    model_id = normalize_model_id(model_id)
    if is_eap_model(model_id):
        return EapLiveGemini(model=model_id)
    return model_id


def build_generate_content_config(
    model_id: str,
) -> types.GenerateContentConfig | None:
    """Per-model generation config, or None to leave ADK's default alone.

    Only clever-chatter needs one: `RunConfig` has no `thinking_config` field,
    but `Gemini.connect()` copies `llm_request.config.thinking_config` into the
    LiveConnectConfig, and `llm_request.config` is the agent's
    `generate_content_config`. That is the only route to thinking_config in a
    live session.
    """
    if not supports_thinking(model_id):
        return None
    # Read at call time, not import time: agent.py calls load_dotenv() after
    # importing this module.
    thinking_level = os.getenv("THINKING_LEVEL", DEFAULT_THINKING_LEVEL).strip().upper()
    return types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
    )
