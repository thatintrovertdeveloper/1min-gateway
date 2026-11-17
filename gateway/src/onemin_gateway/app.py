"""FastAPI app: the OpenAI-compatible surface OpenCode talks to.

This is the only place that knows about HTTP. Everything else (protocol
translation, backend calls, cost tracking) is plain, testable functions.

Responsibilities, and only these:
- Expose `POST /v1/chat/completions` (streaming + non-streaming) and
  `GET /v1/models`.
- Run ONE round of the translation loop per request: flatten the incoming
  messages + tools into a prompt, call the configured backend, parse the
  result for a tool_call, and translate it back into OpenAI's native shape.
  If the model emitted more than one tool_call in a single response, the
  extras are queued and a round may instead dispense a queued call with no
  backend call at all (see `tool_queue.py`).
- Track cost/usage.

It does NOT execute tools, does NOT loop multiple tool calls itself (that's
OpenCode's job -- it calls this endpoint again with the tool result appended
to `messages` for each step), and does NOT do model routing.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import protocol
from .backends import BackendError, call_backend
from .config import GatewayConfig, usage_path
from .cost import UsageTracker, estimate_tokens
from .models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChoiceMessage,
    FunctionCall,
    Model,
    ModelList,
    ToolCall,
    Usage,
    new_id,
    now,
)
from .tool_queue import ToolCallQueue

logger = logging.getLogger("onemin_gateway")

app = FastAPI(title="1min.ai Agent Gateway", version="0.1.0")


def _record_first_attempt_usage(
    session_id: str, model: str, attempt: object, submitted_prompt: str
) -> None:
    """Persist the first attempt's cost when a corrective retry fails."""
    reported_in = getattr(attempt, "prompt_tokens", None)
    reported_out = getattr(attempt, "completion_tokens", None)
    if reported_in is not None and reported_out is not None:
        prompt_tokens, completion_tokens = reported_in, reported_out
    else:
        prompt_tokens, completion_tokens = (
            estimate_tokens(submitted_prompt),
            estimate_tokens(getattr(attempt, "text", "")),
        )
    usage_tracker.record(session_id, model, prompt_tokens, completion_tokens)


def _openai_error(status: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"message": message, "type": kind}}
    )


def _safe_validation_message(
    errors: list[dict[str, object]],
) -> str:
    """Name the offending field without echoing the submitted value."""
    fields = []
    for error in errors:
        location = ".".join(
            str(part) for part in error.get("loc", ()) if part != "body"
        )
        if location and location not in fields:
            fields.append(location)
    if fields:
        return "Invalid request field(s): " + ", ".join(fields[:5])
    return "Invalid request body"


@app.exception_handler(RequestValidationError)
async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return _openai_error(400, _safe_validation_message(exc.errors()), "invalid_request_error")


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Return OpenAI-style JSON for HTTP errors (including method errors)."""
    kind = "server_error" if exc.status_code >= 500 else "invalid_request_error"
    return _openai_error(exc.status_code, str(exc.detail), kind)


# Loaded once at process start. The gateway is stateless *between requests*
# except for config + usage tracking, both of which live here.
config = GatewayConfig.load()
usage_tracker = UsageTracker(usage_path(), config)
tool_call_queue = ToolCallQueue()

# Header OpenCode's plugin sets (via the `chat.headers` hook) so the gateway
# can attribute cost/usage to a session without OpenCode's `sessionID`
# otherwise being present anywhere in the OpenAI wire format.
SESSION_HEADER = "x-opencode-session-id"


def _session_id(request: Request, body: ChatCompletionRequest) -> str:
    header_val = request.headers.get(SESSION_HEADER)
    if header_val:
        return header_val
    return body.session_id or "default"


async def _run_one_round(
    body: ChatCompletionRequest, session_id: str
) -> tuple[ChatCompletionResponse, str]:
    """Run exactly one prompt -> backend -> parse round, with the one
    corrective retry described in PROMPT.md. Returns the response plus the
    raw backend text actually used (for logging).

    If the previous round's response contained more than one tool_call
    block, the extras are queued (see `tool_queue.py`); this round dispenses
    the next queued call, if any, without calling the backend at all."""
    # Only a tool result as the most recent message acknowledges a queued
    # call. Older tool results in the history must not unlock a call on a
    # later, unrelated turn.
    last = body.messages[-1]
    acknowledged_id = (
        last.tool_call_id if last.role == "tool" and last.tool_call_id else None
    )
    queued = tool_call_queue.pop_next(
        session_id, {acknowledged_id} if acknowledged_id else None
    )
    if queued is not None:
        logger.info(
            "Dispensing queued tool_call %r for session %s without a backend "
            "call (%d remaining)",
            queued.name,
            session_id,
            tool_call_queue.pending_count(session_id),
        )
        tool_call = ToolCall(
            id=new_id("call"),
            function=FunctionCall(name=queued.name, arguments=json.dumps(queued.arguments)),
        )
        # The next queued call must wait for this one's result, not for the
        # original backend response's tool_call id.
        tool_call_queue.rebase_next(session_id, tool_call.id)
        choice = Choice(
            message=ChoiceMessage(content=None, tool_calls=[tool_call]),
            finish_reason="tool_calls",
        )
        usage = Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        response = ChatCompletionResponse(model=body.model, choices=[choice], usage=usage)
        return response, ""

    tools = body.tools or []
    prompt = protocol.build_prompt(body.messages, tools)

    try:
        result = await call_backend(prompt, body.model, config)
    except BackendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    parsed_calls, remaining_text = protocol.parse_tool_calls(result.text)
    parsed = parsed_calls[0] if parsed_calls else None
    # Keep both attempts' reported usage; a corrective retry costs money too.
    attempts = [result]
    prompts = [prompt]

    if parsed is None and tools and config.tool_call_retry:
        if protocol.looks_like_failed_attempt(result.text):
            logger.info(
                "Model response looks like a failed tool_call attempt "
                "(malformed JSON, narrated intent, or raw code) -- retrying "
                "once with correction: %r",
                result.text[:200],
            )
            retry_prompt = (
                prompt + "\n" + result.text + "\n\nHuman: " + protocol.RETRY_CORRECTION_PROMPT
            )
            try:
                result = await call_backend(retry_prompt, body.model, config)
            except BackendError as exc:
                # The first attempt was already billed; record it before
                # surfacing the retry failure.
                _record_first_attempt_usage(session_id, body.model, attempts[0], prompts[0])
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            attempts.append(result)
            prompts.append(retry_prompt)
            parsed_calls, remaining_text = protocol.parse_tool_calls(result.text)
            parsed = parsed_calls[0] if parsed_calls else None

    prompt_tokens = 0
    completion_tokens = 0
    for attempt, submitted_prompt in zip(attempts, prompts):
        if attempt.prompt_tokens is not None and attempt.completion_tokens is not None:
            prompt_tokens += attempt.prompt_tokens
            completion_tokens += attempt.completion_tokens
        else:
            prompt_tokens += estimate_tokens(submitted_prompt)
            completion_tokens += estimate_tokens(attempt.text)

    usage_tracker.record(session_id, body.model, prompt_tokens, completion_tokens)
    usage = Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )

    if parsed is not None:
        tool_call = ToolCall(
            id=new_id("call"),
            function=FunctionCall(
                name=parsed.name, arguments=json.dumps(parsed.arguments)
            ),
        )
        if len(parsed_calls) > 1:
            overflow = parsed_calls[1:]
            tool_call_queue.extend(session_id, overflow, tool_call.id)
            logger.info(
                "Parsed %d tool_call blocks from one response for session %s; "
                "returning %r now and queuing %d more: %s",
                len(parsed_calls),
                session_id,
                parsed_calls[0].name,
                len(overflow),
                [c.name for c in overflow],
            )
            if len(parsed_calls) == protocol.DEFAULT_MAX_TOOL_CALLS_PER_RESPONSE:
                logger.warning(
                    "Hit the %d tool_call safety cap for session %s -- further "
                    "calls beyond this point may have been ignored",
                    protocol.DEFAULT_MAX_TOOL_CALLS_PER_RESPONSE,
                    session_id,
                )
        logger.debug(
            "Dispatching tool_call %r for session %s with arguments: %s",
            parsed.name,
            session_id,
            {
                k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v)
                for k, v in parsed.arguments.items()
            },
        )
        choice = Choice(
            message=ChoiceMessage(content=None, tool_calls=[tool_call]),
            finish_reason="tool_calls",
        )
    else:
        # No tool call found (either none was needed, or both attempts
        # failed to produce one) -- return the text as-is so the loop
        # doesn't hang, per PROMPT.md's robustness requirement.
        choice = Choice(
            message=ChoiceMessage(content=result.text or remaining_text),
            finish_reason="stop",
        )

    response = ChatCompletionResponse(model=body.model, choices=[choice], usage=usage)
    return response, result.text


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _openai_error(400, "Invalid JSON request body", "invalid_request_error")
    try:
        body = ChatCompletionRequest.model_validate(payload)
    except ValidationError as exc:
        return _openai_error(
            400, _safe_validation_message(exc.errors()), "invalid_request_error"
        )
    if not body.messages:
        return _openai_error(400, "messages must not be empty", "invalid_request_error")
    # Header attribution is the default, but an explicit body session id
    # still wins for direct API clients (see models.py).
    session_id = body.session_id or _session_id(request, body)

    response, _ = await _run_one_round(body, session_id)

    if not body.stream:
        return response.model_dump(exclude_none=True)

    return StreamingResponse(
        _fake_sse_stream(response), media_type="text/event-stream"
    )


async def _fake_sse_stream(response: ChatCompletionResponse) -> AsyncIterator[bytes]:
    """Emit `response` as an OpenAI-compatible SSE stream.

    The gateway always calls the backend non-streaming internally (see
    backends.py for why), so this "streams" the already-complete response as
    a couple of chunks rather than token-by-token. OpenCode's AI SDK client
    accumulates chunks the same way regardless of how many there are.
    """
    choice = response.choices[0]
    base = {
        "id": response.id,
        "object": "chat.completion.chunk",
        "created": now(),
        "model": response.model,
    }

    # First chunk: role.
    yield _sse(
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    )

    if choice.message.tool_calls:
        # OpenAI-compatible SDKs buffer tool-call deltas by `index` and only
        # commit a call once `function.name` arrives. Keeping id/type/name and
        # arguments in separate chunks is the canonical stream shape.
        for i, call in enumerate(choice.message.tool_calls):
            yield _sse(
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": i, "id": call.id, "type": "function"}
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            )
            yield _sse(
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": i,
                                        "function": {
                                            "name": call.function.name,
                                            "arguments": "",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            )
            yield _sse(
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": i,
                                        "function": {
                                            "arguments": call.function.arguments
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            )
    elif choice.message.content:
        yield _sse(
            {
                **base,
                "choices": [
                    {"index": 0, "delta": {"content": choice.message.content}, "finish_reason": None}
                ],
            }
        )

    yield _sse(
        {
            **base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": choice.finish_reason}],
            "usage": response.usage.model_dump(),
        }
    )
    yield b"data: [DONE]\n\n"


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode("utf-8")


@app.get("/v1/models")
async def list_models() -> dict:
    model_ids = sorted(config.pricing.keys())
    return ModelList(data=[Model(id=m) for m in model_ids]).model_dump()


@app.get("/v1/usage")
async def usage(request: Request) -> dict:
    session_id = request.headers.get(SESSION_HEADER) or "default"
    payload = usage_tracker.snapshot()
    if session_id:
        payload["current_session"] = usage_tracker.session_snapshot(session_id)
    return payload


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "backend": config.backend}
