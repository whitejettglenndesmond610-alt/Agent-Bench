import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { ComparePage } from "./ComparePage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function response(body: object) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

function result(score: number, rank: number, benchmark = false) {
  return {
    status: "evaluated",
    rank,
    score,
    source_agent_version_id: "source-version",
    source_version: 1,
    source_snapshot_id: "snapshot-1",
    source_run_id: "run-1",
    source_run_name: "正式评测",
    source_profile_key: "unified-v3",
    source_published_at: "2026-07-16T08:00:00Z",
    benchmark_count: 1,
    success_rate: 98,
    anomaly_rate: 2,
    http_call_count: 201,
    benchmark_scores: benchmark ? [{
      benchmark_id: "benchmark-1",
      name: "C-Eval",
      ability: "reasoning",
      language: "zh",
      score
    }] : []
  };
}

function agent(id: string, name: string, offset: number) {
  return {
    agent_id: id,
    agent_version_id: `version-${id}`,
    agent_name: name,
    provider: "Provider",
    version: offset + 1,
    metrics: {
      overall: result(90 - offset, offset + 1),
      reasoning: result(91 - offset, offset + 1, true),
      retrieval: result(82 + offset, offset + 1),
      data_reasoning: result(87, offset + 1),
      instruction_following: result(84 + offset, offset + 1),
      long_context: result(79 + offset, offset + 1)
    }
  };
}

function renderPage(entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}><ComparePage /></MemoryRouter>
    </QueryClientProvider>
  );
}

test("renders global metrics, benchmarks, and a fixed-range radar chart", async () => {
  const firstId = "11111111-1111-4111-8111-111111111111";
  const secondId = "22222222-2222-4222-8222-222222222222";
  const fetchMock = vi.fn().mockResolvedValue(response({
    generated_at: "2026-07-16T08:00:00Z",
    eligible_agent_count: 2,
    protocols: [
      { metric: "overall", protocol_hash: "hash" },
      { metric: "reasoning", protocol_hash: "hash" },
      { metric: "retrieval", protocol_hash: "hash" },
      { metric: "data_reasoning", protocol_hash: "hash" },
      { metric: "instruction_following", protocol_hash: "hash" },
      { metric: "long_context", protocol_hash: "hash" }
    ],
    agents: [agent(firstId, "Agent Alpha", 0), agent(secondId, "Agent Beta", 1)]
  }));
  vi.stubGlobal("fetch", fetchMock);
  renderPage(`/leaderboard/compare?agents=${firstId},${secondId}`);

  expect(await screen.findByRole("heading", { name: "Agent 横向对比" })).toBeInTheDocument();
  expect(screen.getByRole("img")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "五项能力雷达" })).toBeInTheDocument();
  expect(screen.getByText("固定范围 0—100")).toBeInTheDocument();
  expect(screen.getAllByText("Agent Alpha").length).toBeGreaterThan(1);
  expect(screen.getByText("C-Eval")).toBeInTheDocument();
  expect(screen.getByText("最近评测 HTTP 调用")).toBeInTheDocument();
  expect(screen.getAllByText("指令遵循").length).toBeGreaterThan(0);
  expect(screen.getAllByText("长上下文").length).toBeGreaterThan(0);
  expect(String(fetchMock.mock.calls[0][0])).toContain("agent_ids=11111111");
});

test("rejects a comparison without two unique agents before calling the API", () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  renderPage("/leaderboard/compare?agents=agent-1");

  expect(screen.getByText(/对比参数无效/)).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
});
