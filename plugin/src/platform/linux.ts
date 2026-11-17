/**
 * Linux service registration: a systemd user unit in
 * ~/.config/systemd/user/, enabled and started via `systemctl --user`.
 */

import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { Shell, SYSTEMD_UNIT } from "./common.js";

function unitDir(): string {
  const xdgConfig = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config");
  return path.join(xdgConfig, "systemd", "user");
}

function unitPath(): string {
  return path.join(unitDir(), SYSTEMD_UNIT);
}

/** systemd splits unquoted ExecStart values on spaces. */
function systemdPath(value: string): string {
  if (/[\r\n\0]/.test(value)) throw new Error("Invalid control character in service path");
  // Inside double quotes systemd only treats \" and \\ specially.
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function unitContents(pythonExe: string, args: string[]): string {
  const command = [`"${systemdPath(pythonExe)}"`, ...args.map((arg) => `"${systemdPath(arg)}"`)].join(" ");
  return `[Unit]
Description=1min.ai Agent Gateway

[Service]
ExecStart=${command}
Restart=on-failure

[Install]
WantedBy=default.target
`;
}

export async function registerLinuxService(
  $: Shell,
  pythonExe: string,
  args: string[],
): Promise<void> {
  const target = unitPath();
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.writeFile(target, unitContents(pythonExe, args), "utf-8");

  const reload = await $`systemctl --user daemon-reload`.quiet().nothrow();
  if (reload.exitCode !== 0) throw new Error(`systemctl daemon-reload failed (${reload.exitCode})`);
  const enable = await $`systemctl --user enable --now ${SYSTEMD_UNIT}`.quiet().nothrow();
  if (enable.exitCode !== 0) throw new Error(`systemctl enable failed (${enable.exitCode})`);
}

export async function isLinuxServiceRegistered(): Promise<boolean> {
  try {
    await fs.access(unitPath());
    return true;
  } catch {
    return false;
  }
}

export async function unregisterLinuxService($: Shell): Promise<void> {
  await $`systemctl --user disable --now ${SYSTEMD_UNIT}`.quiet().nothrow();
  await fs.rm(unitPath(), { force: true });
}

/**
 * Restart the systemd user unit. Needed after every gateway source code
 * change (see restartWindowsService for why).
 */
export async function restartLinuxService($: Shell): Promise<void> {
  const result = await $`systemctl --user restart ${SYSTEMD_UNIT}`.quiet().nothrow();
  if (result.exitCode !== 0) throw new Error(`systemctl restart failed (${result.exitCode})`);
}
