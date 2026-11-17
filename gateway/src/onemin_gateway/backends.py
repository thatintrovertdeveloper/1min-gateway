"""Model backends: 1min.ai directly, or the user's own OpenAI-compatible relay.

Both backends are called *non-streaming* internally, even when OpenCode asks
for a streamed response: the gateway must see the model's full text before it
can tell whether it contains a `{"tool_call": ...}` object, so there is no
way to usefully stream partial tokens through the tool-call parser anyway.
When the client requested `stream: true`, `app.py` fakes an SSE stream by
emitting the fully-formed response as a single chunk. This keeps the
translation loop simple, per the "don't over-engineer" directive -- real
token-by-token streaming can be layered on later without changing this
interface.

Backend call signature: `call(prompt, model) -> BackendResult`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import GatewayConfig

logger = logging.getLogger("onemin_gateway")


def _response_excerpt(resp: httpx.Response) -> str:
    """Log only the status: bodies can carry keys or prompt data."""
    return f"HTTP {resp.status_code}"


def _json_object(resp: httpx.Response, backend: str) -> dict[str, Any]:
    """Parse and validate a backend JSON response shape."""
    try:
        data = resp.json()
    except ValueError as exc:
        raise BackendError(f"{backend} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise BackendError(f"{backend} returned an unexpected JSON value")
    return data


class BackendError(Exception):
    """Raised when a backend call fails; message is safe to surface to the
    client as an OpenAI-style error."""


@dataclass
class BackendResult:
    text: str
    # Real usage from the backend, if it reported any (see 1min.ai notes
    # below). None means "the caller should estimate tokens instead".
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


async def call_oneminai(prompt: str, model: str, config: GatewayConfig) -> BackendResult:
    """Call 1min.ai's unified chat endpoint.

    Wire format (validated against a production relay, since this is
    undocumented and not covered by 1min.ai's public docs beyond the
    endpoint name):

    - `POST {base_url}/api/chat-with-ai`
    - headers: `API-KEY: <key>`, `Content-Type: application/json`
    - body: `{"type": "UNIFY_CHAT_WITH_AI", "model": <model>,
      "promptObject": {"prompt": <flat prompt>,
      "settings": {"historySettings": {"isMixed": false},
      "withMemories": false}}}`
    - response text: `aiRecord.aiRecordDetail.resultObject[0]`, falling back
      to a top-level `content` field.
    - response usage (when present): `aiRecord.metadata.{inputToken,
      outputToken,totalToken}`. The upstream sometimes reports an all-zero
      metadata block for a real exchange -- that's treated as "no usage
      reported" so the caller falls back to a local estimate instead of
      recording a confident zero.
    """
    if not config.oneminai_api_key:
        raise BackendError(
            "1min.ai backend selected but no API key configured. Set "
            "'oneminai_api_key' in the gateway config."
        )

    url = f"{config.oneminai_base_url.rstrip('/')}{config.oneminai_chat_endpoint}"
    headers = {
        "API-KEY": config.oneminai_api_key,
        "Content-Type": "application/json",
    }
    body = {
        "type": "UNIFY_CHAT_WITH_AI",
        "model": model,
        "promptObject": {
            "prompt": prompt,
            "settings": {
                "historySettings": {"isMixed": False},
                "withMemories": False,
            },
        },
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            # httpx exception strings can contain the full request URL,
            # including credentials embedded in a custom backend base URL.
            logger.warning("Network error calling 1min.ai: %s", exc)
            raise BackendError("Network error calling 1min.ai") from exc

    if resp.status_code >= 400:
        logger.warning("1min.ai error %s: %s", resp.status_code, _response_excerpt(resp))
        raise BackendError(f"1min.ai returned HTTP {resp.status_code}")

    data = _json_object(resp, "1min.ai")
    ai_record = data.get("aiRecord") or {}
    if not isinstance(ai_record, dict):
        raise BackendError("1min.ai returned an unexpected response shape")
    detail = ai_record.get("aiRecordDetail") or {}
    if not isinstance(detail, dict):
        raise BackendError("1min.ai returned an unexpected response shape")
    result_object = detail.get("resultObject") or []
    if not isinstance(result_object, (list, tuple)):
        raise BackendError("1min.ai returned an unexpected resultObject")
    raw_text = result_object[0] if result_object else data.get("content")
    if raw_text is not None and not isinstance(raw_text, str):
        raise BackendError("1min.ai returned a non-text model response")
    text = raw_text or ""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    metadata = ai_record.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise BackendError("1min.ai returned an unexpected metadata shape")
    input_tok = metadata.get("inputToken")
    output_tok = metadata.get("outputToken")
    if isinstance(input_tok, (int, float)) or isinstance(output_tok, (int, float)):
        pt = int(input_tok or 0)
        ct = int(output_tok or 0)
        # A real exchange never costs exactly 0 tokens both ways -- treat an
        # all-zero report as "not accounted for" rather than a confident
        # zero (matches the known upstream quirk).
        if pt != 0 or ct != 0:
            prompt_tokens, completion_tokens = pt, ct

    return BackendResult(
        text=text, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )


async def call_relay(prompt: str, model: str, config: GatewayConfig) -> BackendResult:
    """Call the user's own OpenAI-compatible relay as an alternative backend.

    The relay is treated the same way as 1min.ai for consistency and easy
    debugging: the whole conversation (including the injected tool-call
    protocol block) is flattened into a single prompt string and sent as one
    user message, rather than assuming the relay has special handling for a
    multi-turn `messages` array. If real usage comes back in the OpenAI
    `usage` field, it's used; otherwise the caller estimates.
    """
    url = f"{config.relay_base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if config.relay_api_key:
        headers["Authorization"] = f"Bearer {config.relay_api_key}"

    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            logger.warning("Network error calling relay: %s", exc)
            raise BackendError("Network error calling relay") from exc

    if resp.status_code >= 400:
        logger.warning("Relay error %s: %s", resp.status_code, _response_excerpt(resp))
        raise BackendError(f"Relay returned HTTP {resp.status_code}")

    data = _json_object(resp, "Relay")
    choices = data.get("choices") or []
    if not isinstance(choices, (list, tuple)):
        raise BackendError("Relay returned an unexpected choices field")
    text = ""
    if choices:
        first = choices[0]
        message = first.get("message") or {} if isinstance(first, dict) else {}
        if not isinstance(message, dict):
            raise BackendError("Relay returned an unexpected message shape")
        raw_text = message.get("content")
        if raw_text is not None and not isinstance(raw_text, str):
            raise BackendError("Relay returned a non-text model response")
        text = raw_text or ""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage = data.get("usage")
    if isinstance(usage, dict):
        pt = usage.get("prompt_tokens")
        ct = usage.get("completion_tokens")
        if isinstance(pt, int) and isinstance(ct, int):
            prompt_tokens, completion_tokens = pt, ct

    return BackendResult(
        text=text, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )


async def call_backend(prompt: str, model: str, config: GatewayConfig) -> BackendResult:
    """Dispatch to whichever backend is configured."""
    if config.backend == "relay":
        return await call_relay(prompt, model, config)
    return await call_oneminai(prompt, model, config)
