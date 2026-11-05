"""Local AI mode: any OpenAI-compatible server (Ollama, LM Studio, vLLM, llama.cpp).

Ported from openshorts/llm_backend.py (MIT, (c) 2024 OpenShorts): ask for ``json_schema``
first, fall back to ``json_object`` and then to no ``response_format``; validate the answer with
the same pydantic schema. Set LLM_BASE_URL (e.g. http://localhost:11434/v1) and LLM_MODEL.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from pydantic import BaseModel

from ...errors import AnalysisError
from ...ingest.source import SourceMeta
from ...transcribe.models import Transcript
from .. import llm_pass
from ..schemas import AnalyzeOptions, Moment
from ..signals import Signals
from .gemini import _parse_text

log = logging.getLogger("snipmint.analyze.local_llm")

DEFAULT_MODEL = "qwen2.5:14b"


def base_url() -> str:
    return (os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/")


def _formats(schema: type[BaseModel]):
    yield {"type": "json_schema", "json_schema": {"name": schema.__name__.lower(), "schema": schema.model_json_schema()}}
    yield {"type": "json_object"}
    yield None


def make_call(client: httpx.Client, url: str, model: str):
    headers = {"Authorization": f"Bearer {(os.environ.get('LLM_API_KEY') or 'ollama').strip()}"}

    def call(prompt: str, schema: type[BaseModel]) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": "You answer with a single JSON object and nothing else."},
            {"role": "user", "content": prompt},
        ]
        data = None
        for fmt in _formats(schema):
            body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.2, "stream": False}
            if fmt is not None:
                body["response_format"] = fmt
            resp = client.post(f"{url}/chat/completions", json=body, headers=headers)
            if fmt is not None and resp.status_code in (400, 422) and "format" in resp.text.lower():
                continue
            if resp.status_code >= 400:
                raise AnalysisError(f"Local AI server {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            break
        if data is None:
            raise AnalysisError("The local AI server rejected every response format.")
        msg = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if isinstance(msg, list):
            msg = "".join(p.get("text", "") for p in msg if isinstance(p, dict))
        try:
            return schema.model_validate(_parse_text(msg)).model_dump()
        except (ValueError, json.JSONDecodeError) as exc:
            raise AnalysisError(f"The local AI answered with invalid JSON: {exc}") from exc

    return call


def pick_moments(
    transcript: Transcript, signals: Signals, meta: SourceMeta, opts: AnalyzeOptions,
    *, client: httpx.Client | None = None,
) -> list[Moment]:  # fmt: skip
    url = base_url()
    if not url:
        raise AnalysisError("LLM_BASE_URL is not set.")
    model = (os.environ.get("LLM_MODEL") or "").strip() or DEFAULT_MODEL
    timeout = float(os.environ.get("LLM_TIMEOUT") or 600)
    batch = int(os.environ.get("LLM_SCORE_BATCH") or 3)  # local contexts are small
    own = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        return llm_pass.pick_moments(make_call(client, url, model), "local_llm", transcript, signals, meta,
                                     opts, batch_size=batch)  # fmt: skip
    finally:
        if own:
            client.close()
