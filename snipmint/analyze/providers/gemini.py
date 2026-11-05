"""Advanced mode: Gemini picks the moments and writes titles, hooks and descriptions.

Needs ``pip install "snipmint[gemini]"`` and an API key (from the UI or GEMINI_API_KEY).
Only transcript text and a few numbers per window are sent; never the video.
The call/retry/block handling follows openshorts/main.py ``_run_gemini_stage`` and
openshorts/gemini_worker.py ``raise_if_blocked`` (MIT, (c) 2024 OpenShorts).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from pydantic import BaseModel

from ...errors import AnalysisError, DependencyMissing
from ...ingest.source import SourceMeta
from ...transcribe.models import Transcript
from .. import llm_pass
from ..schemas import AnalyzeOptions, Moment
from ..signals import Signals

log = logging.getLogger("snipmint.analyze.gemini")

DEFAULT_MODEL = "gemini-3.1-flash-lite"
INSTALL_HINT = 'Install with: pip install "snipmint[gemini]"'
_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}
_TRANSIENT = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL", "overloaded",
              "Deadline", "empty response", "JSON")  # fmt: skip
_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


def model_name() -> str:
    return (os.environ.get("GEMINI_MODEL") or "").strip() or DEFAULT_MODEL


def redact(text: str) -> str:
    """Never let an API key reach a log line or an error message."""
    return _KEY_PATTERN.sub("AIza…(hidden)", text)


def make_client(api_key: str):
    try:
        from google import genai
    except ImportError as exc:
        raise DependencyMissing("google-genai", INSTALL_HINT) from exc
    return genai.Client(api_key=api_key)


def _parse_text(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("Gemini returned an empty response body.")
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Gemini response did not contain a JSON object.")
    return json.loads(text[start : end + 1])


def _raise_if_blocked(response: Any) -> None:
    pf = getattr(response, "prompt_feedback", None)
    reason = getattr(pf, "block_reason", None)
    if reason:
        raise llm_pass.BlockedError(f"Gemini blocked this content ({getattr(reason, 'name', reason)}).")
    for c in getattr(response, "candidates", None) or []:
        fr = getattr(c, "finish_reason", None)
        name = (getattr(fr, "name", None) or str(fr or "")).upper()
        if name in _BLOCKED:
            raise llm_pass.BlockedError(f"Gemini blocked its answer ({name}).")


def make_call(client: Any, model: str, sleep=time.sleep):
    """A ``call(prompt, schema)`` for llm_pass with structured output and transient retries."""

    def call(prompt: str, schema: type[BaseModel]) -> dict[str, Any]:
        try:
            from google.genai import types as gtypes

            config: Any = gtypes.GenerateContentConfig(response_mime_type="application/json",
                                                       response_schema=schema)  # fmt: skip
        except ImportError:  # tests with a fake client
            config = {"response_mime_type": "application/json", "response_schema": schema}
        for attempt in range(1, 4):
            try:
                response = client.models.generate_content(model=model, contents=prompt, config=config)
                _raise_if_blocked(response)
                parsed = getattr(response, "parsed", None)
                if parsed is not None:
                    data = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
                else:
                    data = _parse_text(getattr(response, "text", "") or "")
                return schema.model_validate(data).model_dump()
            except llm_pass.BlockedError:
                raise
            except Exception as exc:
                msg = redact(str(exc))
                if attempt == 3 or not any(t in msg for t in _TRANSIENT):
                    raise AnalysisError(f"Gemini error: {msg[:300]}") from None
                wait = 3 * (2 ** (attempt - 1))
                log.warning("Gemini transient error (attempt %d/3), retrying in %ds: %s", attempt, wait, msg[:150])
                sleep(wait)
        raise AnalysisError("Gemini did not answer.")

    return call


def pick_moments(
    transcript: Transcript, signals: Signals, meta: SourceMeta, opts: AnalyzeOptions,
    *, api_key: str | None = None, client: Any = None,
) -> list[Moment]:  # fmt: skip
    key = (api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if client is None:
        if not key:
            raise AnalysisError("No Gemini API key.")
        client = make_client(key)
    model = model_name()
    log.info("Analysing with Gemini (%s)", model)
    batch = int(os.environ.get("LLM_SCORE_BATCH") or 8)
    return llm_pass.pick_moments(make_call(client, model), "gemini", transcript, signals, meta, opts,
                                 batch_size=batch)  # fmt: skip


def check_key(api_key: str) -> tuple[bool, str | None]:
    """Cheap validity check: list models with this key."""
    try:
        client = make_client(api_key)
        models = client.models.list(config={"page_size": 5})
        next(iter(models), None)
        return True, None
    except DependencyMissing as exc:
        return False, str(exc)
    except Exception as exc:
        msg = redact(str(exc))
        if "API_KEY_INVALID" in msg or "API key not valid" in msg or "PERMISSION_DENIED" in msg:
            return False, "Google did not accept this key."
        return False, f"Could not check the key: {msg[:200]}"
