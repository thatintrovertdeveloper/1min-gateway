"""Configuration loading for the gateway.

Config lives in a cross-platform location so both the gateway (run as a
background service) and the OpenCode plugin (which writes/reads it during
install) can agree on where to find it:

- Windows: ``%APPDATA%\\1min-gateway\\config.json``
- macOS / Linux: ``~/.config/1min-gateway/config.json``

The gateway is otherwise stateless between requests; the only other file it
touches is the usage/cost log (see ``cost.py``), which lives next to the
config file so both survive restarts.

Never hardcode path separators -- everything goes through ``pathlib.Path``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("onemin_gateway")

# Mirrors plugin/src/gateway-config.ts:DEFAULT_PRICING and the sample config.
# Existing configs override these entries; new/unknown model ids are not
# removed, and users can still add their own pricing.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {"input": 1305, "output": 2612},
    "qwen3-8b": {"input": 0, "output": 0},
    "qwen3.7-flash": {"input": 90, "output": 390},
    "deepseek-flash": {"input": 420, "output": 840},
    "grok-4.3": {"input": 3752, "output": 7505},
    "us.anthropic.claude-sonnet-5": {"input": 6603, "output": 33017},
}


def config_dir() -> Path:
    """Return the cross-platform config directory, creating it if needed."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        root = Path(xdg) if xdg else Path.home() / ".config"
    directory = root / "1min-gateway"
    # The config holds API keys; keep the directory private on POSIX.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def config_path() -> Path:
    return config_dir() / "config.json"


def _make_private(path: Path) -> None:
    """Best-effort POSIX permissions; Windows ignores the mode bits."""
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _quarantine_bad_config(path: Path) -> None:
    """Keep a corrupt config instead of silently destroying its API key."""
    stamp = f"{int(time.time())}.{os.getpid()}"
    backup = path.with_name(f"{path.name}.bad.{stamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bad.{stamp}.{suffix}")
        suffix += 1
    try:
        path.replace(backup)
        logger.error("Saved unreadable config to %s", backup)
    except OSError as exc:
        logger.error("Could not back up unreadable config %s: %s", path, exc)


def usage_path() -> Path:
    return config_dir() / "usage.json"


@dataclass
class ModelPricing:
    """Price per 1M tokens. Units are whatever the user's config uses
    (credits, USD, etc.) -- the gateway is unit-agnostic and just multiplies.
    """

    input: float = 0.0
    output: float = 0.0


@dataclass
class GatewayConfig:
    # Which backend to call: "oneminai" (default) or "relay".
    backend: str = "oneminai"

    # 1min.ai backend.
    oneminai_api_key: str = ""
    oneminai_base_url: str = "https://api.1min.ai"
    oneminai_chat_endpoint: str = "/api/chat-with-ai"

    # User's own OpenAI-compatible relay, used when backend == "relay".
    relay_base_url: str = "http://192.168.50.206:5001/v1"
    relay_api_key: str = ""

    # Network.
    host: str = "127.0.0.1"
    port: int = 8765

    # Per-model pricing, keyed by model id, price per 1,000,000 tokens.
    pricing: dict[str, ModelPricing] = field(
        default_factory=lambda: {
            model: ModelPricing(input=values["input"], output=values["output"])
            for model, values in DEFAULT_PRICING.items()
        }
    )

    # If a model isn't in `pricing`, fall back to this (defaults to free/0
    # so an unknown model never silently produces a misleading non-zero
    # cost).
    default_pricing: ModelPricing = field(default_factory=ModelPricing)

    # Retry the model once with a corrective prompt if it fails to emit a
    # parseable tool_call/plain-text response.
    tool_call_retry: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> "GatewayConfig":
        path = path or config_path()
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cls._with_env_overrides(cfg)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config root must be a JSON object")
            cfg = cls.from_dict(raw)
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            # Never overwrite a config we cannot read: preserve it for the
            # user, then start from safe defaults so the service still boots.
            logger.error("Could not load config %s: %s", path, exc)
            _quarantine_bad_config(path)
            cfg = cls()
            cfg.save(path)
        return cls._with_env_overrides(cfg)

    @staticmethod
    def _with_env_overrides(cfg: "GatewayConfig") -> "GatewayConfig":
        """Allow secrets to come from the environment instead of disk."""
        key = os.environ.get("ONEMIN_GATEWAY_API_KEY")
        relay_key = os.environ.get("ONEMIN_GATEWAY_RELAY_API_KEY")
        if key:
            cfg.oneminai_api_key = key
        if relay_key:
            cfg.relay_api_key = relay_key
        return cfg

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GatewayConfig":
        pricing_raw = raw.get("pricing") or {}
        if not isinstance(pricing_raw, dict):
            raise ValueError("'pricing' must be a JSON object")
        custom_pricing = {}
        for model, entry in pricing_raw.items():
            if not isinstance(model, str) or not isinstance(entry, dict):
                logger.warning("Ignoring malformed pricing entry for %r", model)
                continue
            try:
                custom_pricing[model] = ModelPricing(
                    input=float(entry.get("input", 0)),
                    output=float(entry.get("output", 0)),
                )
            except (TypeError, ValueError):
                logger.warning("Ignoring malformed pricing entry for %r", model)
        pricing = {
            model: ModelPricing(input=values["input"], output=values["output"])
            for model, values in DEFAULT_PRICING.items()
        }
        pricing.update(custom_pricing)

        default_pricing_raw = raw.get("default_pricing") or {}
        if not isinstance(default_pricing_raw, dict):
            raise ValueError("'default_pricing' must be a JSON object")
        default_pricing = ModelPricing(
            input=float(default_pricing_raw.get("input", 0)),
            output=float(default_pricing_raw.get("output", 0)),
        )
        return cls(
            backend=raw.get("backend", "oneminai"),
            oneminai_api_key=raw.get("oneminai_api_key", ""),
            oneminai_base_url=raw.get("oneminai_base_url", "https://api.1min.ai"),
            oneminai_chat_endpoint=raw.get("oneminai_chat_endpoint", "/api/chat-with-ai"),
            relay_base_url=raw.get("relay_base_url", "http://192.168.50.206:5001/v1"),
            relay_api_key=raw.get("relay_api_key", ""),
            host=raw.get("host", "127.0.0.1"),
            port=int(raw.get("port", 8765)),
            pricing=pricing,
            default_pricing=default_pricing,
            tool_call_retry=bool(raw.get("tool_call_retry", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "oneminai_api_key": self.oneminai_api_key,
            "oneminai_base_url": self.oneminai_base_url,
            "oneminai_chat_endpoint": self.oneminai_chat_endpoint,
            "relay_base_url": self.relay_base_url,
            "relay_api_key": self.relay_api_key,
            "host": self.host,
            "port": self.port,
            "pricing": {
                model: {"input": p.input, "output": p.output}
                for model, p in self.pricing.items()
            },
            "default_pricing": {
                "input": self.default_pricing.input,
                "output": self.default_pricing.output,
            },
            "tool_call_retry": self.tool_call_retry,
        }

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Atomic write: a crash must never leave a half-written config that
        # the next launch would mistake for "no config" and overwrite.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
            _make_private(tmp)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        _make_private(path)

    def pricing_for(self, model: str) -> ModelPricing:
        return self.pricing.get(model, self.default_pricing)
