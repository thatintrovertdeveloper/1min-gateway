/**
 * macOS service registration: a per-user LaunchAgent plist in
 * ~/Library/LaunchAgents/, loaded with launchctl.
 */

import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { Shell, LAUNCHD_LABEL } from "./common.js";

function plistPath(): string {
  return path.join(os.homedir(), "Library", "LaunchAgents", `${LAUNCHD_LABEL}.plist`);
}

function xmlEscape(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function plistContents(pythonExe: string, args: string[]): string {
  const programArgs = [pythonExe, ...args]
    .map((value) => `    <string>${xmlEscape(value)}</string>`)
    .join("\n");
  const logDir = path.join(os.homedir(), "Library", "Logs");
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
${programArgs}
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${xmlEscape(path.join(logDir, "onemin-gateway.log"))}</string>
  <key>StandardErrorPath</key>
  <string>${xmlEscape(path.join(logDir, "onemin-gateway.err.log"))}</string>
</dict>
</plist>
`;
}

export async function registerMacService(
  $: Shell,
  pythonExe: string,
  args: string[],
): Promise<void> {
  const target = plistPath();
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.mkdir(path.join(os.homedir(), "Library", "Logs"), { recursive: true });
  await fs.writeFile(target, plistContents(pythonExe, args), "utf-8");

  // Unload first (idempotent re-install) then load, ignoring unload errors
  // if it wasn't loaded yet. Load failures must be visible to the installer.
  await $`launchctl unload ${target}`.quiet().nothrow();
  const load = await $`launchctl load -w ${target}`.quiet().nothrow();
  if (load.exitCode !== 0) throw new Error(`launchctl load failed (${load.exitCode})`);
}

export async function isMacServiceRegistered(): Promise<boolean> {
  try {
    await fs.access(plistPath());
    return true;
  } catch {
    return false;
  }
}

export async function unregisterMacService($: Shell): Promise<void> {
  const target = plistPath();
  await $`launchctl unload ${target}`.quiet().nothrow();
  await fs.rm(target, { force: true });
}

/**
 * Restart the LaunchAgent. Needed after every gateway source code change
 * (see restartWindowsService for why) -- unload/load cycles the process so
 * it re-imports the updated modules.
 */
export async function restartMacService($: Shell): Promise<void> {
  const target = plistPath();
  await $`launchctl unload ${target}`.quiet().nothrow();
  const load = await $`launchctl load -w ${target}`.quiet().nothrow();
  if (load.exitCode !== 0) {
    // If the unload failed because the job was already loaded, load -w
    // reports "already loaded". Kick the running job instead.
    const kick = await $`launchctl kickstart -k system/${LAUNCHD_LABEL}`.quiet().nothrow();
    if (kick.exitCode !== 0) throw new Error(`launchctl restart failed (${load.exitCode})`);
  }
}
