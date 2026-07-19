import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { AgentPlazaDetailPage, AgentPlazaPage } from "./AgentPlazaPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function response(body: object) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
}

const agents = [{
  agent_id: "agent-a",
  agent_version_id: "version-a",
  name: "Agent Alpha",
  provider: "Provider A",
  description: "带引用的研究助手",
  logo_url: null,
  version: 3,
  max_prompt_characters: 10000,
  supports_citations: true,
  is_favorite: false
}, {
  agent_id: "agent-b",
  agent_version_id: "version-b",
  name: "Agent Beta",
  provider: "Provider B",
  description: "写作助手",
  logo_url: null,
  version: 2,
  max_prompt_characters: null,
  supports_citations: false,
  is_favorite: true
}];

function overviewMetric(score: number | null = null, rank: number | null = null) {
  return {
    status: score == null ? "not_evaluated" : "evaluated",
    rank,
    score,
    source_published_at: score == null ? null : "2026-07-19T08:00:00Z",
    benchmark_count: score == null ? 0 : 2,
    success_rate: score == null ? null : 98,
    anomaly_rate: score == null ? null : 2,
    http_call_count: score == null ? null : 100,
    latency_p50_ms: score == null ? null : 120,
    latency_p95_ms: score == null ? null : 350,
    benchmark_scores: []
  };
}

test("filters the plaza and persists an Agent favorite", async () => {
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response(agents));
    if (input === "/api/v1/leaderboards/global/overview") return Promise.resolve(response({
      agents: [{
        agent_id: "agent-a",
        metrics: { overall: overviewMetric(91, 1), reasoning: overviewMetric(90, 1), retrieval: overviewMetric(), data_reasoning: overviewMetric(), instruction_following: overviewMetric(), long_context: overviewMetric() }
      }]
    }));
    if (input === "/api/v1/workspace/agents/agent-a/favorite" && init?.method === "PUT") return Promise.resolve(response({}));
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<QueryClientProvider client={client()}><MemoryRouter><AgentPlazaPage userId="user-1" /></MemoryRouter></QueryClientProvider>);

  expect(await screen.findByRole("heading", { name: "Agent 广场" })).toBeInTheDocument();
  expect(await screen.findByText("Agent Alpha")).toBeInTheDocument();
  expect(screen.getByText("Agent Beta")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox", { name: "支持引用" }));
  expect(screen.getByText("Agent Alpha")).toBeInTheDocument();
  expect(screen.queryByText("Agent Beta")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "收藏 Agent Alpha" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/agents/agent-a/favorite",
    expect.objectContaining({ method: "PUT" })
  ));
  expect(await screen.findByRole("button", { name: "取消收藏 Agent Alpha" })).toBeInTheDocument();
});

test("renders a public Agent profile with six metrics and score history", async () => {
  const history = [{
    score: 91,
    protocol_hash: "protocol-1",
    protocol_matches_current: true,
    is_current_source: true,
    agent_version_id: "version-a",
    version: 3,
    source_snapshot_id: "snapshot-1",
    source_run_id: "run-1",
    source_run_name: "七月正式评测",
    source_profile_key: "unified-v3",
    source_published_at: "2026-07-19T08:00:00Z",
    benchmark_count: 2
  }];
  const empty: never[] = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response(agents));
    if (input === "/api/v1/leaderboards/agents/agent-a?history_limit=50") return Promise.resolve(response({
      agent_id: "agent-a",
      agent_version_id: "version-a",
      agent_name: "Agent Alpha",
      provider: "Provider A",
      description: "公开研究助手",
      version: 3,
      endpoint_url: "https://secret.example/run",
      metrics: { overall: overviewMetric(91, 1), reasoning: overviewMetric(90, 1), retrieval: overviewMetric(), data_reasoning: overviewMetric(), instruction_following: overviewMetric(), long_context: overviewMetric() },
      score_history: { overall: history, reasoning: empty, retrieval: empty, data_reasoning: empty, instruction_following: empty, long_context: empty }
    }));
    throw new Error(`Unexpected request: ${input}`);
  }));
  render(
    <QueryClientProvider client={client()}>
      <MemoryRouter initialEntries={["/plaza/agent-a"]}>
        <Routes><Route path="/plaza/:agentId" element={<AgentPlazaDetailPage userId="user-1" />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );

  expect(await screen.findByRole("heading", { name: "Agent Alpha" })).toBeInTheDocument();
  expect(screen.getAllByText("综合分").length).toBeGreaterThan(0);
  expect(screen.getByRole("img", { name: /综合分正式分数趋势/ })).toBeInTheDocument();
  expect(screen.getAllByText("七月正式评测").length).toBeGreaterThan(0);
  expect(screen.getByRole("link", { name: "在线运行" })).toHaveAttribute("href", "/workspace?agentVersion=version-a");
  expect(screen.queryByText("https://secret.example/run")).not.toBeInTheDocument();
});
