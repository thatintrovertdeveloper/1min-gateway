# PROJECT KNOWLEDGE BASE

**Generated:** 2026-11-18

## OVERVIEW

Project: **1min.ai Agent Gateway + OpenCode Plugin** (`1min-gateway`)

A cross-platform system that lets OpenCode (and other OpenAI-compatible coding
agents) do **real tool calling — including MCP tools — while using 1min.ai** as
the model backend. 1min.ai's `chat-with-ai` endpoint (`type:
UNIFY_CHAT_WITH_AI`) takes a flat prompt string and returns text: no `tools`
parameter, no structured `tool_calls`. This project emulates tool calling in a
translation gateway that injects tool definitions as a JSON protocol block,
parses `{"tool_call": {...}}` objects out of the model's text, and returns
native OpenAI `tool_calls` for OpenCode to execute.

Stack:
- **Gateway** (`gateway/`): Python >= 3.11, FastAPI >= 0.115, uvicorn >= 0.30,
  httpx >= 0.27, pydantic >= 2.7 (v2). Packaged with hatchling; console script
  `1min-gateway`. Dev extras: pytest >= 8, pytest-asyncio >= 0.23.
- **Plugin** (`plugin/`): TypeScript (ES2022, strict, `moduleResolution:
  Bundler`), `@opencode-ai/plugin` ^1.18.31, `tsc` build to `dist/`.
- Spec source of truth: `PROMPT.md`. Root `README.md` is user-facing docs.

## STRUCTURE

```
.
├── AGENTS.md                  This file.
├── PROMPT.md                  Original build specification (requirements source).
├── README.md                  User-facing install/config/architecture docs.
├── gateway/                   Python FastAPI service (installable package).
│   ├── pyproject.toml         Deps, hatchling build, console script, pytest config.
│   ├── config.sample.json     Reference config with pricing filled in.
│   ├── README.md              Short pointer to root README.
│   ├── src/onemin_gateway/
│   │   ├── app.py             FastAPI surface: HTTP only. /v1/chat/completions,
│   │   │                      /v1/models, /v1/usage, /health. One translation
│   │   │                      round per request; fake SSE streaming.
│   │   ├── protocol.py        Translation core: build_prompt, flatten_messages,
│   │   │                      parse_tool_calls, looks_like_failed_attempt,
│   │   │                      double-escaped-whitespace repair. Pure string logic.
│   │   ├── backends.py        call_oneminai / call_relay / call_backend -> BackendResult.
│   │   ├── config.py          GatewayConfig dataclass, cross-platform config paths.
│   │   ├── cost.py            UsageTracker (thread-safe, persisted), estimate_tokens.
│   │   ├── models.py          Pydantic request/response models (OpenAI subset).
│   │   ├── tool_queue.py      Per-session FIFO for extra tool_calls in one response.
│   │   └── __main__.py        `1min-gateway` entry point (uvicorn, stdio redirect).
│   └── tests/                 pytest suite (pytest-asyncio, asyncio_mode = "auto").
│       ├── test_protocol.py   52 tests — parsing, flattening, prompt building.
│       ├── test_backends.py   12 tests — 1min.ai + relay wire formats.
│       ├── test_cost.py       12 tests — token/cost math, persistence.
│       ├── test_app.py        4 tests — queueing/dispensing, usage isolation.
│       └── test_tool_queue.py 8 tests — FIFO behavior.
├── plugin/                    OpenCode plugin (TypeScript).
│   ├── package.json           @opencode-ai/plugin dep; build = tsc.
│   ├── tsconfig.json          ES2022, strict, outDir dist, declarations.
│   └── src/
│       ├── index.ts           Plugin entry: config hook registers provider,
│       │                      chat.headers hook tags session ID.
│       ├── install.ts         Idempotent cross-platform installer + restartGateway().
│       ├── config-paths.ts    Cross-platform config/venv paths (mirrors Python).
│       ├── gateway-config.ts  Config read/write + DEFAULT_PRICING.
│       ├── port.ts            findFreePort, isGatewayHealthy.
│       └── platform/          Per-OS service registration:
│           ├── common.ts      SERVICE_NAME = "OneMinGateway",
│           │                  LAUNCHD_LABEL = "com.oneminai.gateway",
│           │                  SYSTEMD_UNIT = "onemin-gateway.service".
│           ├── windows.ts     Scheduled Task.
│           ├── macos.ts       LaunchAgent plist.
│           └── linux.ts       systemd user unit.
└── scripts/
    └── restart-gateway.ps1    Windows helper: end/start Scheduled Task + poll /health.
```

## COMMANDS

| Action | Command |
|--------|---------|
| Install gateway (editable + dev deps) | `cd gateway && python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"` |
| Run gateway | `1min-gateway` (or `python -m onemin_gateway`); `--host`, `--port`, `--log-level` flags |
| Test gateway | `cd gateway && pytest` |
| Build plugin | `cd plugin && npm install && npm run build` |
| Watch-build plugin | `cd plugin && npm run watch` |
| Manual restart (Windows) | `powershell -File scripts/restart-gateway.ps1` |
| Health check | `curl http://127.0.0.1:8765/health` |

Config/usage live at `%APPDATA%\1min-gateway\` (Windows) or
`~/.config/1min-gateway/` (macOS/Linux): `config.json`, `usage.json`,
`gateway.log`, and the installer's `venv/`.

## CODING STANDARDS

- **Language**: Python 3.11+ (uses `X | None`, `from __future__ import
  annotations` everywhere); TypeScript ES2022 strict mode.
- **Style**: Type-hinted, dataclasses for internal value types, Pydantic v2
  models at the HTTP boundary (`extra="allow"` to tolerate AI-SDK fields).
  Docstrings explain *why* and constraints, not just what.
- **Architecture rule**: `app.py` is the only HTTP-aware module. `protocol.py`
  is pure string translation — no filesystem, no tool execution, no network.
  The gateway never executes tools, never routes models, never touches the
  filesystem beyond config/usage. OpenCode owns tool execution (built-ins +
  MCP), permissions, and approvals.
- **Paths**: `pathlib.Path` (Python) / `node:path` (TS) everywhere; never
  hardcode separators. Plugin and gateway must mirror each other's config-path
  logic exactly (`plugin/src/config-paths.ts` ↔
  `gateway/src/onemin_gateway/config.py`).
- **Config merge safety**: never clobber an existing `config.json` (the user's
  API key). Only fill in missing fields; preserve `backend`, pricing, and key.
  Built-in `DEFAULT_PRICING` is merged under user overrides in both the plugin
  and gateway. Unreadable configs are quarantined as
  `config.json.bad.<timestamp>` rather than overwritten. Writes are atomic and
  files use mode `0600` (dir `0700`) on POSIX. Secrets may also be set via
  `ONEMIN_GATEWAY_API_KEY` / `ONEMIN_GATEWAY_RELAY_API_KEY`.
- **Backend calls are always non-streaming internally**; streaming responses
  are faked as SSE chunks after the full text is parsed. Keep it that way
  unless the parser is redesigned.
- **Tests**: pytest + pytest-asyncio (`asyncio_mode = "auto"` in
  `pyproject.toml`). Tests must isolate module-level state — see
  `gateway/tests/test_app.py`'s `isolate_app_state` autouse fixture, which
  monkeypatches `app_module.usage_tracker` and `app_module.tool_call_queue`
  because `app.py` loads them once at import time. Use `tmp_path`/`monkeypatch`.
  No test suite exists for the plugin.
- **Formatting**: no configured linter/formatter; match the existing
  hand-formatted style. Plugin imports use explicit `.js` extensions on
  relative specifiers (ESM/Bundler resolution).

## WHERE TO LOOK

- **Source (gateway)**: `gateway/src/onemin_gateway/` — start with `app.py`
  then `protocol.py`.
- **Source (plugin)**: `plugin/src/index.ts` then `install.ts`.
- **Tests**: `gateway/tests/`.
- **Docs**: `README.md` (user-facing), `PROMPT.md` (original spec),
  `gateway/README.md`, `plugin/README.md`.
- **Config reference**: `gateway/config.sample.json` and
  `plugin/src/gateway-config.ts` (`DEFAULT_PRICING`).

## NOTES

- **Model pass-through**: the gateway does NOT route models. It forwards the
  request's `model` field unchanged to the backend. Tiered model selection is
  configured as named subagents in OpenCode's `opencode.json`.
- **Session attribution**: OpenCode's session ID is not in the OpenAI wire
  format. The plugin's `chat.headers` hook sets `x-opencode-session-id`; the
  gateway reads it for `GET /v1/usage` per-session stats. A `session_id` field
  on the request body is also honored if present (it takes precedence over
  the header for direct API clients).
- **Multiple tool calls per response**: `parse_tool_calls` returns up to
  `DEFAULT_MAX_TOOL_CALLS_PER_RESPONSE` (8). The first is returned now; extras
  go to a per-session in-memory FIFO (`tool_queue.py`) and are dispensed on
  later requests with zero token usage and no backend call only when the client
  echoes the preceding `tool_call_id` in a `role: "tool"` message as the last
  message in the conversation. Queues are
  transient (no persistence); entries expire after `DEFAULT_QUEUE_TTL_SECONDS
  = 300`.
- **Corrective retry**: if no tool_call is parsed but
  `looks_like_failed_attempt()` is true (malformed JSON, narrated intent, or
  raw code/shell), the backend is called once more with
  `RETRY_CORRECTION_PROMPT`; if that fails, text is returned with
  `finish_reason: "stop"` so the agent loop never hangs.
- **Double-escaped whitespace repair**: weak models write `\\n`/`\\t` where a
  real newline/tab was meant in `content`/`oldString`/`newString` arguments.
  `protocol._repair_double_escaped_whitespace` only rewrites when the value
  has no real newline/tab and >= 2 escape occurrences — deliberately narrow so
  Windows paths (legitimate backslashes) are never mangled.
- **Prompt reminders**: `TASK_PERSISTENCE_REMINDER` and
  `NEWLINE_ESCAPING_REMINDER` are re-appended immediately before the
  `Assistant:` generation cue on every round, not only at the top, to counter
  "lost in the middle" across tool round-trips. Keep that placement.
- **Token usage**: prefer backend-reported usage; 1min.ai's all-zero
  `metadata.inputToken/outputToken` is treated as "not reported" and falls back
  to `chars / 4` (`CHARS_PER_TOKEN = 4`). Corrective retries accumulate usage
  from both attempts (estimated against the exact prompts sent). Cost =
  `tokens / 1_000_000 * price` from per-model pricing, falling back to
  `default_pricing` (0).
- **Restart required after gateway source edits**: uvicorn runs without
  `--reload` and the plugin's `installGateway()` is a no-op when `/health`
  already responds, so an editable `pip install -e` never reloads a running
  process. Use `restartGateway()` (plugin) or `scripts/restart-gateway.ps1`
  (Windows) to pick up changes. This is intentional.
- **Plugin provider registration** (verified against `@opencode-ai/plugin`
  1.18.x): write `Config.provider["1min-gateway"] = { npm:
  "@ai-sdk/openai-compatible", options: { baseURL, apiKey: "not-needed" }, models:
  ... }` from the `config` hook. `tool_call: true` per model; `cost` from
  pricing. The gateway holds the real 1min.ai key.
- **OpenCode subagent caveats** (documented in README): built-in `general` /
  `explore` subagents ignore model overrides; a named subagent entry must
  include `"mode": "subagent"` explicitly alongside `model`.
- **Service names**: Scheduled Task `OneMinGateway` (Windows), LaunchAgent
  `com.oneminai.gateway` (macOS), systemd user unit `onemin-gateway.service`
  (Linux). Default port 8765; installer picks the next free port and persists
  it if taken.
- **Constraints**: keep gateway dependencies minimal; do not add tool execution,
  model routing, or safety gates; do not over-engineer. Git working tree is
  clean on `main`.
