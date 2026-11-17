/**
 * Read/write the gateway's config.json, mirroring
 * gateway/src/onemin_gateway/config.py's shape. Reads are merge-safe: an
 * existing config (in particular the user's pasted API key) is never
 * clobbered by a re-install -- only missing fields are filled in.
 */

import { promises as fs } from "node:fs";
import path from "node:path";

export interface ModelPricing {
  input: number;
  output: number;
}

export interface GatewayFileConfig {
  backend: "oneminai" | "relay";
  oneminai_api_key: string;
  oneminai_base_url: string;
  oneminai_chat_endpoint: string;
  relay_base_url: string;
  relay_api_key: string;
  host: string;
  port: number;
  tool_call_retry: boolean;
  pricing: Record<string, ModelPricing>;
  default_pricing: ModelPricing;
}

// Kept in sync with PROMPT.md's reference pricing and
// gateway/config.sample.json.
export const DEFAULT_PRICING: Record<string, ModelPricing> = {
  "deepseek-v4-pro": { input: 1305, output: 2612 },
  "qwen3-8b": { input: 0, output: 0 },
  "qwen3.7-flash": { input: 90, output: 390 },
  "deepseek-flash": { input: 420, output: 840 },
  "grok-4.3": { input: 3752, output: 7505 },
  "us.anthropic.claude-sonnet-5": { input: 6603, output: 33017 },
};

export function defaultConfig(port: number): GatewayFileConfig {
  return {
    backend: "oneminai",
    oneminai_api_key: "",
    oneminai_base_url: "https://api.1min.ai",
    oneminai_chat_endpoint: "/api/chat-with-ai",
    relay_base_url: "http://192.168.50.206:5001/v1",
    relay_api_key: "",
    host: "127.0.0.1",
    port,
    tool_call_retry: true,
    pricing: DEFAULT_PRICING,
    default_pricing: { input: 0, output: 0 },
  };
}

export type ConfigReadResult =
  | { kind: "ok"; config: GatewayFileConfig }
  | { kind: "missing" }
  | { kind: "corrupt"; message: string };

/**
 * Distinguish "no config yet" from an unreadable one: callers must never
 * overwrite a corrupt config, which would destroy the user's API key.
 */
export async function readConfigResult(filePath: string): Promise<ConfigReadResult> {
  let raw: string;
  try {
    raw = await fs.readFile(filePath, "utf-8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return { kind: "missing" };
    return { kind: "corrupt", message: (error as Error).message };
  }
  try {
    const parsed = JSON.parse(raw) as GatewayFileConfig;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("config root must be a JSON object");
    }
    return { kind: "ok", config: parsed };
  } catch (error) {
    return { kind: "corrupt", message: (error as Error).message };
  }
}

/** Back-compat: null only when the config does not exist. */
export async function readConfig(filePath: string): Promise<GatewayFileConfig | null> {
  const result = await readConfigResult(filePath);
  return result.kind === "ok" ? result.config : null;
}

export async function writeConfig(filePath: string, config: GatewayFileConfig): Promise<void> {
  const dir = path.dirname(filePath);
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  if (process.platform !== "win32") {
    try {
      await fs.chmod(dir, 0o700);
    } catch {
      // Best effort (e.g. filesystem without POSIX permissions).
    }
  }
  const tmp = `${filePath}.${process.pid}.tmp`;
  try {
    await fs.writeFile(tmp, JSON.stringify(config, null, 2), { encoding: "utf-8", mode: 0o600 });
    await fs.rename(tmp, filePath);
  } finally {
    await fs.rm(tmp, { force: true });
  }
  if (process.platform !== "win32") {
    try {
      await fs.chmod(filePath, 0o600);
    } catch {
      // Best effort (e.g. filesystem without POSIX permissions).
    }
  }
}

/**
 * Ensure a config file exists, preserving anything already there (API key,
 * backend choice, custom pricing edits) and only filling in what's missing.
 * Returns the effective config actually on disk after this call.
 */
export async function ensureConfig(filePath: string, preferredPort: number): Promise<GatewayFileConfig> {
  const result = await readConfigResult(filePath);
  if (result.kind === "corrupt") {
    const backup = `${filePath}.bad.${Date.now()}`;
    await fs.rename(filePath, backup);
    throw new Error(`Unreadable config saved as ${backup}: ${result.message}`);
  }
  if (result.kind === "ok") {
    // Fill in missing fields without touching user values. Keep stale custom
    // model entries; they are harmless and may still be valid on their relay.
    const defaults = defaultConfig(preferredPort);
    const existing = result.config;
    const pricing: Record<string, ModelPricing> = { ...DEFAULT_PRICING };
    for (const [model, entry] of Object.entries(existing.pricing ?? {})) {
      if (
        entry &&
        typeof entry === "object" &&
        Number.isFinite(entry.input) &&
        Number.isFinite(entry.output)
      ) {
        pricing[model] = entry;
      }
    }
    const dp = existing.default_pricing;
    const defaultPricing =
      dp && typeof dp === "object" && Number.isFinite(dp.input) && Number.isFinite(dp.output)
        ? dp
        : defaults.default_pricing;
    const merged: GatewayFileConfig = {
      ...defaults,
      ...existing,
      pricing,
      default_pricing: defaultPricing,
    };
    await writeConfig(filePath, merged);
    return merged;
  }
  const fresh = defaultConfig(preferredPort);
  await writeConfig(filePath, fresh);
  return fresh;
}
