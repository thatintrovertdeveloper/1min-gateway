/**
 * Cross-platform gateway installer, run on the plugin's first activation.
 *
 * Steps:
 * 1. Bootstrap Python: prefer `uv` (pinned Python + venv management), fall
 *    back to system `python3`/`python` + the stdlib `venv` module.
 * 2. `pip install -e` the sibling `gateway/` package (editable, so the same
 *    checkout that ships the plugin also runs the server).
 * 3. Ensure a config.json exists (never clobbering an existing API key).
 * 4. Pick a free port (default 8765) and persist it.
 * 5. Register the gateway as a background service for the current OS and
 *    start it.
 * 6. Health-check until it responds, or report that it didn't.
 *
 * Idempotent: safe to call on every OpenCode startup. If the gateway is
 * already running and healthy, everything except the health check is
 * skipped.
 */

import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { configFilePath, venvDir, venvPython } from "./config-paths.js";
import type { GatewayFileConfig } from "./gateway-config.js";
import { ensureConfig, readConfigResult } from "./gateway-config.js";
import { registerLinuxService, restartLinuxService } from "./platform/linux.js";
import { registerMacService, restartMacService } from "./platform/macos.js";
import { registerWindowsService, restartWindowsService } from "./platform/windows.js";
import type { Shell } from "./platform/common.js";
import { findFreePort, isGatewayHealthy } from "./port.js";

export interface InstallResult {
  port: number;
  baseUrl: string;
  alreadyRunning: boolean;
}

export interface RestartResult {
  healthy: boolean;
  baseUrl: string;
  message: string;
}

export type Logger = (message: string) => void;

/**
 * Locate the gateway's Python source. Defaults to the sibling `gateway/`
 * directory in this repo (this plugin and the gateway are developed and
 * shipped together). Override with `ONE_MIN_GATEWAY_SRC` if the plugin is
 * ever installed from a location where the gateway source lives elsewhere
 * (e.g. published to npm separately from a PyPI-published gateway).
 */
export function gatewaySourceDir(): string {
  if (process.env.ONE_MIN_GATEWAY_SRC) return process.env.ONE_MIN_GATEWAY_SRC;
  const here = path.dirname(fileURLToPath(import.meta.url));
  // Local dev: dist/install.js -> ../../gateway (repo root/gateway).
  // Published package: dist/install.js -> ../gateway (vendored in the tarball).
  const sibling = path.join(here, "..", "gateway");
  const bundled = path.join(here, "..", "..", "gateway");
  return existsSync(sibling) ? sibling : bundled;
}

async function commandWorks($: Shell, cmd: string, ...args: string[]): Promise<boolean> {
  try {
    const result = await $`${cmd} ${args}`.quiet().nothrow();
    return result.exitCode === 0;
  } catch {
    return false;
  }
}

async function bootstrapPython($: Shell, log: Logger): Promise<string> {
  const vDir = venvDir();
  const vPython = venvPython();
  const src = gatewaySourceDir();

  if (await commandWorks($, "uv", "--version")) {
    log("1min-gateway: bootstrapping Python via uv...");
    const venv = await $`uv venv ${vDir} --python 3.11`.quiet().nothrow();
    if (venv.exitCode !== 0) {
      throw new Error(`Could not create gateway venv at ${vDir} (exit ${venv.exitCode})`);
    }
    await $`uv pip install --python ${vPython} -e ${src}`.quiet();
    return vPython;
  }

  let systemPython: string | null = null;
  if (await commandWorks($, "python3", "--version")) systemPython = "python3";
  else if (await commandWorks($, "python", "--version")) systemPython = "python";

  if (!systemPython) {
    throw new Error(
      "1min-gateway: no Python found. Install Python 3.11+, or install " +
        "`uv` (https://astral.sh/uv), then try again.",
    );
  }

  log(`1min-gateway: uv not found; falling back to ${systemPython} + venv...`);
  const venv = await $`${systemPython} -m venv ${vDir}`.quiet().nothrow();
  if (venv.exitCode !== 0) {
    throw new Error(`Could not create gateway venv at ${vDir} (exit ${venv.exitCode})`);
  }
  await $`${vPython} -m pip install -e ${src}`.quiet();
  return vPython;
}

async function registerService(
  $: Shell,
  pythonExe: string,
  gatewayArgs: string[],
  log: Logger,
): Promise<void> {
  log(`1min-gateway: registering background service for ${process.platform}...`);
  if (process.platform === "win32") {
    await registerWindowsService($, pythonExe, gatewayArgs);
  } else if (process.platform === "darwin") {
    await registerMacService($, pythonExe, gatewayArgs);
  } else {
    await registerLinuxService($, pythonExe, gatewayArgs);
  }
}

/** Service commands must use the exact port persisted in config.json. */
function gatewayArgs(config: GatewayFileConfig): string[] {
  return ["-m", "onemin_gateway", "--port", String(config.port)];
}

function baseUrlFor(host: string, port: number): string {
  return `http://${host}:${port}/v1`;
}

export async function installGateway($: Shell, log: Logger = () => {}): Promise<InstallResult> {
  const cfgPath = configFilePath();
  const read = await readConfigResult(cfgPath);
  if (read.kind === "corrupt") {
    throw new Error(`Refusing to overwrite unreadable config: ${read.message}`);
  }
  const existing = read.kind === "ok" ? read.config : null;
  const configuredHost = existing?.host ?? "127.0.0.1";
  // 0.0.0.0 is a bind address; clients must reach the loopback interface.
  const host = configuredHost === "0.0.0.0" ? "127.0.0.1" : configuredHost;

  if (existing && (await isGatewayHealthy(host, existing.port))) {
    log(`1min-gateway: already running on port ${existing.port}.`);
    return {
      port: existing.port,
      baseUrl: baseUrlFor(host, existing.port),
      alreadyRunning: true,
    };
  }

  const port = await findFreePort(existing?.port ?? 8765, host);
  const config = await ensureConfig(cfgPath, port);

  if (!config.oneminai_api_key && config.backend === "oneminai") {
    log(
      `1min-gateway: no API key set yet. Edit ${cfgPath} and set ` +
        "'oneminai_api_key', then restart the gateway service.",
    );
  }

  const pythonExe = await bootstrapPython($, log);
  await registerService($, pythonExe, gatewayArgs(config), log);

  const baseUrl = baseUrlFor(host, config.port);
  for (let attempt = 0; attempt < 10; attempt++) {
    if (await isGatewayHealthy(host, config.port)) {
      log(`1min-gateway: healthy at ${baseUrl}`);
      return { port: config.port, baseUrl, alreadyRunning: false };
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }

  throw new Error(
    `Service registered but ${baseUrl}/health did not respond within 10s. ` +
      "Check the service logs.",
  );
}

/**
 * Restart the gateway's background service.
 *
 * `installGateway()` is deliberately a no-op once the gateway is already
 * healthy, so plain source edits (even with an editable `pip install -e`)
 * never take effect on their own -- the running Python process keeps its
 * already-imported modules until the process itself is restarted, and
 * `__main__.py` runs uvicorn without `--reload`. This is the explicit,
 * on-demand way to pick up a code change: stop the service, start it, wait
 * for `/health` to come back.
 */
export async function restartGateway($: Shell, log: Logger = () => {}): Promise<RestartResult> {
  const cfgPath = configFilePath();
  const read = await readConfigResult(cfgPath);
  if (read.kind === "corrupt") {
    throw new Error(`Refusing to restart with unreadable config: ${read.message}`);
  }
  const config = read.kind === "ok" ? read.config : null;
  const configuredHost = config?.host ?? "127.0.0.1";
  const host = configuredHost === "0.0.0.0" ? "127.0.0.1" : configuredHost;
  const port = config?.port ?? 8765;
  const baseUrl = baseUrlFor(host, port);

  log(`1min-gateway: restarting service for ${process.platform}...`);
  if (process.platform === "win32") {
    await restartWindowsService($);
  } else if (process.platform === "darwin") {
    await restartMacService($);
  } else {
    await restartLinuxService($);
  }

  for (let attempt = 0; attempt < 10; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    if (await isGatewayHealthy(host, port)) {
      const message = `1min-gateway: restarted and healthy at ${baseUrl}`;
      log(message);
      return { healthy: true, baseUrl, message };
    }
  }

  const message =
    `1min-gateway: restart issued but ${baseUrl}/health did not respond ` +
    "within 10s. Check gateway.log in the config directory.";
  log(message);
  return { healthy: false, baseUrl, message };
}
