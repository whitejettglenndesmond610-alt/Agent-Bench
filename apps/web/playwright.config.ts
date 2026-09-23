import { defineConfig } from "@playwright/test";

import { apiOrigin, backendEnv, ports, repoRoot, webOrigin } from "./e2e/env";
import { isTestRun, prepareStack } from "./e2e/stack";

// Playwright launches webServers before globalSetup, so the disposable stack
// (sqlite migrations, fake-Agent seed, worker) is prepared here at config
// load instead; the worker is stopped again in global-teardown.ts.
if (isTestRun()) {
  prepareStack();
}

const backendServers = [
  {
    command: `uv run uvicorn agent_eval.demo.fake_services:app --host 127.0.0.1 --port ${ports.fakeAgent}`,
    url: `http://127.0.0.1:${ports.fakeAgent}/health`,
    env: { ...backendEnv, FAKE_AGENT_VARIANT: "alpha" }
  },
  {
    command: `uv run uvicorn agent_eval.demo.fake_services:app --host 127.0.0.1 --port ${ports.fakeJudge}`,
    url: `http://127.0.0.1:${ports.fakeJudge}/health`,
    env: { ...backendEnv, FAKE_AGENT_VARIANT: "beta" }
  },
  {
    command: `uv run uvicorn agent_eval.api.main:app --host 127.0.0.1 --port ${ports.api}`,
    url: `${apiOrigin}/health/ready`,
    env: backendEnv
  }
].map((server) => ({
  ...server,
  cwd: repoRoot,
  timeout: 120_000,
  reuseExistingServer: !process.env.CI
}));

export default defineConfig({
  testDir: "./e2e",
  globalTeardown: "./e2e/global-teardown.ts",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: webOrigin,
    trace: "retain-on-failure"
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: [
    ...backendServers,
    {
      command: `pnpm exec vite preview --host 127.0.0.1 --port ${ports.web} --strictPort`,
      url: webOrigin,
      cwd: import.meta.dirname,
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
      env: { E2E_API_ORIGIN: apiOrigin }
    }
  ]
});
