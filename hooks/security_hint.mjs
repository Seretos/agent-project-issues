/**
 * UserPromptSubmit hook: periodically reminds the agent to surface the
 * .seretos/ managed-settings deny-rule guidelines when a .seretos/ directory
 * is present in the project tree.
 *
 * Runs under Node (the Claude Code runtime) — NOT Python: this plugin ships as
 * a self-contained PyInstaller binary, so end-user installs have no Python
 * toolchain. Every consumer does have Node, which is why the sibling plugins'
 * hooks are Node too. Strictly fail-open: any error exits 0 with no output.
 *
 * Manual verification (from a directory WITH a .seretos ancestor):
 *   echo '{"session_id":"t1","cwd":"<dir>"}' | node hooks/security_hint.mjs
 *     -> first call: prints hint JSON
 *     -> immediate second call (same session_id): prints nothing (rate-limited)
 *   echo not-json | node hooks/security_hint.mjs      -> exit 0, no output
 *   echo '{"cwd":"/tmp"}' | node hooks/security_hint.mjs (no .seretos) -> exit 0
 */

import { createHash } from "node:crypto";
import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

const HINT_TEXT =
  "Security notice: This project uses .seretos/projects.yml to configure" +
  " agent permissions. Inform the user that a Claude Code managed-settings" +
  " deny rule is recommended to protect this file from being modified" +
  " by agents. See" +
  " https://github.com/Seretos/agent-project-issues/blob/main/SECURITY.md" +
  " for setup instructions.";

const FIRE_ON_FIRST = 1; // fire on the 1st qualifying prompt of the session
const FIRE_INTERVAL = 10; // then every 10th prompt thereafter

function hasSeretosAncestor(startDir) {
  let current = resolve(startDir);
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const candidate = join(current, ".seretos");
    try {
      if (existsSync(candidate) && statSync(candidate).isDirectory()) {
        return true;
      }
    } catch {
      // ignore and keep walking up
    }
    const parent = dirname(current);
    if (parent === current) {
      return false; // reached filesystem root
    }
    current = parent;
  }
}

function stateFile(sessionId) {
  return join(tmpdir(), `project-issues-hook-state-${sessionId}.json`);
}

function loadCounter(sessionId) {
  try {
    const data = JSON.parse(readFileSync(stateFile(sessionId), "utf8"));
    const n = Number.parseInt(data.counter, 10);
    return Number.isFinite(n) ? n : 0;
  } catch {
    return 0;
  }
}

function saveCounter(sessionId, counter) {
  try {
    writeFileSync(stateFile(sessionId), JSON.stringify({ counter }), "utf8");
  } catch {
    // best effort
  }
}

function shouldFire(counter) {
  if (counter === FIRE_ON_FIRST) return true;
  return counter > FIRE_ON_FIRST && (counter - FIRE_ON_FIRST) % FIRE_INTERVAL === 0;
}

function main() {
  let event;
  try {
    event = JSON.parse(readFileSync(0, "utf8"));
  } catch {
    process.exit(0);
  }

  const cwd = event.cwd || process.cwd();
  if (!hasSeretosAncestor(String(cwd))) {
    process.exit(0);
  }

  let sessionId = event.session_id;
  if (!sessionId) {
    sessionId = createHash("sha256").update(String(cwd)).digest("hex").slice(0, 16);
  }
  sessionId = String(sessionId);

  const counter = loadCounter(sessionId) + 1;
  saveCounter(sessionId, counter);

  if (!shouldFire(counter)) {
    process.exit(0);
  }

  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "UserPromptSubmit",
        additionalContext: HINT_TEXT,
      },
    }),
  );
}

try {
  main();
} catch {
  process.exit(0);
}
