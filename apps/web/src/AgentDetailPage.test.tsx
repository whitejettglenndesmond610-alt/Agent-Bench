import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { AgentDetailPage } from "./AgentDetailPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function response(body: unknown) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

const metric = (status: string, score: number | null, runId: string | null = null) => ({
  status,
  score,
  run_id: runId,
  source_version: score == null ? null : 3,
  updated_at: "2026-07-19T08:00:00Z"
});

test("shows active and candidate versions, six metrics, safe configuration, and Agent actions", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/agents/agent-1") return Promise.resolve(response({
      id: "agent-1",
      name: "Atlas Agent",
      provider: "Atlas",
      description: "External research Agent",
      logo_url: null,
      status: "enabled",
      created_at: "2026-07-18T08:00:00Z",
      updated_at: "2026-07-19T08:00:00Z",
      versions: [
        {
          id: "version-3", agent_id: "agent-1", version: 3, endpoint_url: "https://agent.example/v1/run",
          auth_type: "bearer", token_configured: true, request_template: "secret-template-marker",
          answer_jsonpath: "$.answer", citations_jsonpath: "$.citations", timeout_seconds: 90,
          concurrency: 4, max_prompt_characters: 8000, lifecycle_status: "active", preflight_status: "passed",
          preflight_report: [{ key: "basic", label: "基础连接", status: "passed", prompt_characters: 10, status_code: 200, latency_ms: 20, error_category: null, error: null }],
          preflight_at: "2026-07-19T07:00:00Z", enabled: true, created_at: "2026-07-19T06:00:00Z"
        },
        {
          id: "version-4", agent_id: "agent-1", version: 4, endpoint_url: "https://agent.example/v1/run-next",
          auth_type: "bearer", token_configured: true, request_template: "next-template",
          answer_jsonpath: "$.answer", citations_jsonpath: null, timeout_seconds: 120,
          concurrency: 2, max_prompt_characters: null, lifecycle_status: "verified", preflight_status: "passed",
          preflight_report: [], preflight_at: "2026-07-19T09:00:00Z", enabled: true, created_at: "2026-07-19T08:30:00Z"
        }
      ]
    }));
    if (input === "/api/v1/evaluation-runs/agent-statuses") return Promise.resolve(response([{
      agent_id: "agent-1", agent_version_id: "version-3", agent_name: "Atlas Agent", provider: "Atlas", version: 3,
      max_prompt_characters: 8000, supports_citations: true,
      comprehensive: metric("evaluated", 88.25, "run-overall"), reasoning: metric("running", null, "run-reasoning"),
      retrieval: metric("evaluated", 84), data_reasoning: metric("needs_retest", 79),
      instruction_following: metric("not_evaluated", null), long_context: metric("failed", null)
    }]));
    if (input === "/api/v1/leaderboards/global/overview") return Promise.resolve(response({
      generated_at: "2026-07-19T08:00:00Z", eligible_agent_count: 1, protocols: [], agents: []
    }));
    throw new Error(`Unexpected request: ${input}`);
  }));

  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/agents/agent-1"]}>
        <Routes><Route path="/agents/:agentId" element={<AgentDetailPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );

  expect(await screen.findByRole("heading", { name: "Atlas Agent" })).toBeInTheDocument();
  expect(screen.getByText("V3")).toBeInTheDocument();
  expect(screen.getByText("V4")).toBeInTheDocument();
  expect(screen.getByText("88.25")).toBeInTheDocument();
  expect(screen.getByText("评测中")).toBeInTheDocument();
  expect(screen.getByText("待复测")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "在线运行" })).toHaveAttribute("href", "/workspace?agentVersion=version-3");
  expect(screen.getByRole("link", { name: "发起评测" })).toHaveAttribute("href", "/runs?launch=1&agentVersion=version-3");
  expect(screen.getByRole("link", { name: "并行对比" })).toHaveAttribute("href", "/compare?agentVersion=version-3");
  expect(screen.getByText("Bearer · 凭据已安全配置")).toBeInTheDocument();
  expect(rendered.container).not.toHaveTextContent("secret-template-marker");
  expect(rendered.container.querySelector('input[type="password"]')).toBeNull();
});
