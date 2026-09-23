import fs from "node:fs";

import { workerPidFile } from "./env";

/** Stop the evaluation worker started in global-setup. */
export default async function globalTeardown() {
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
