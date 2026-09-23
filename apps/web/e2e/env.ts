import path from "node:path";
import { fileURLToPath } from "node:url";

// Shared constants for the Playwright E2E environment. The E2E stack runs a
// sqlite-backed API + worker plus the demo fake agent/judge services, all on
// loopback ports, so no external API is ever contacted.
const webDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

export const repoRoot = path.dirname(path.dirname(webDir));
export const runtimeDir = path.join(webDir, ".e2e");
export const databaseFile = path.join(runtimeDir, "e2e.db");
export const dataDir = path.join(runtimeDir, "data");
export const workerPidFile = path.join(runtimeDir, "worker.pid");
export const workerLogFile = path.join(runtimeDir, "worker.log");

export const ports = {
  api: 8100,
  fakeAgent: 9101,
  fakeJudge: 9102,
  web: 4173
} as const;

export const apiOrigin = `http://127.0.0.1:${ports.api}`;
export const webOrigin = `http://127.0.0.1:${ports.web}`;

export const passwords = {
  readOnly: "e2e-readonly-password",
  operator: "e2e-operator-password"
} as const;

// Test-only credentials; the hashes match the passwords above and are never
// used outside the disposable E2E stack.
export const backendEnv: Record<string, string> = {
  AGENT_EVAL_ENVIRONMENT: "development",
  AGENT_EVAL_DATABASE_URL: `sqlite+aiosqlite:///${databaseFile}`,
  AGENT_EVAL_DATA_DIR: dataDir,
  AGENT_EVAL_LOG_LEVEL: "WARNING",
  AGENT_EVAL_WORKER_POLL_SECONDS: "0.5",
  AGENT_EVAL_WORKSPACE_STREAM_POLL_SECONDS: "0.2",
  AGENT_EVAL_READ_ONLY_PASSWORD_HASH:
    "$argon2id$v=19$m=65536,t=3,p=4$8JtFw7khd+S9RqfZ6Um5+g$0P9cXmlyqr5RTeeKatBNRcoGLzYoN/t6HKMpnf2v7MY",
  AGENT_EVAL_OPERATOR_PASSWORD_HASH:
    "$argon2id$v=19$m=65536,t=3,p=4$b51NMEgajAJeRVgpQ7CEig$FYX/NsNKHnVty2egmds3Uei4ymmm3xPQyTRhCTbcaYc",
  AGENT_EVAL_ENCRYPTION_KEY: "lwwAsqkppQPhadCWWkSob8FgC-BHLejd6BCV6yRVxAo=",
  AGENT_EVAL_DEEPSEEK_API_KEY: "e2e-fake-key",
  AGENT_EVAL_DEEPSEEK_API_URL: `http://127.0.0.1:${ports.fakeJudge}/chat/completions`,
  E2E_FAKE_AGENT_PORT: String(ports.fakeAgent)
};

/** Environment for backend processes with proxy variables scrubbed so local
 * services never route loopback traffic through a corporate proxy. */
export function cleanBackendEnv(): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value === undefined || /^(.*_)?proxy$/i.test(key)) continue;
    env[key] = value;
  }
  return { ...env, ...backendEnv, NO_PROXY: "*", no_proxy: "*" };
}
