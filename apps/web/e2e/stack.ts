import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import {
  cleanBackendEnv,
  dataDir,
  repoRoot,
  runtimeDir,
  workerLogFile,
  workerPidFile
} from "./env";

function stopStaleWorker() {
  let pid: number | undefined;
  try {
    pid = Number(fs.readFileSync(workerPidFile, "utf8").trim());
  } catch {
    return;
  }
  if (!pid) return;
  try {
    process.kill(pid, "SIGTERM");
  } catch {
    // Already gone.
  }
}

/**
 * Prepare a disposable sqlite database, seed the fake Agent, and start the
 * evaluation worker (the only stack component without an HTTP health port, so
 * it cannot be a Playwright webServer). Runs at config load because Playwright
 * starts webServers before globalSetup, and the API health check needs the
 * migrated database.
 */
export function prepareStack() {
  stopStaleWorker();
  fs.rmSync(runtimeDir, { recursive: true, force: true });
  fs.mkdirSync(dataDir, { recursive: true });

  const env = cleanBackendEnv();
  execFileSync("uv", ["run", "alembic", "upgrade", "head"], {
    cwd: repoRoot,
    env,
    stdio: "inherit"
  });
  execFileSync("uv", ["run", "python", path.join("apps", "web", "e2e", "seed_env.py")], {
    cwd: repoRoot,
    env,
    stdio: "inherit"
  });

  const log = fs.openSync(workerLogFile, "w");
  const worker = spawn("uv", ["run", "python", "-m", "agent_eval.worker.main"], {
    cwd: repoRoot,
    env,
    detached: true,
    stdio: ["ignore", log, log]
  });
  fs.writeFileSync(workerPidFile, String(worker.pid));
  fs.closeSync(log);
  worker.unref();
}

/** True when this config load belongs to an actual `playwright test` run
 * (and not `playwright install`, `show-report`, etc.). */
export function isTestRun(argv: string[] = process.argv.slice(2)) {
  return argv[0] === "test";
}
