import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { GlobalLeaderboardMetric, GlobalOverviewMetric } from "./api";
import { LeaderboardPage } from "./LeaderboardPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function response(body: object) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

function metric(
  score: number | null = null,
  rank: number | null = null,
  overrides: Partial<GlobalOverviewMetric> = {}
): GlobalOverviewMetric {
  return {
    status: score === null ? "not_evaluated" : "evaluated",
    rank,
    score,
    source_agent_version_id: score === null ? null : "source-version",
    source_version: score === null ? null : 1,
    source_snapshot_id: score === null ? null : "snapshot-1",
    source_run_id: score === null ? null : "run-1234567890",
    source_run_name: score === null ? null : "正式综合评测",
    source_profile_key: score === null ? null : "unified-v3",
    source_published_at: score === null ? null : "2026-07-15T08:00:00Z",
    benchmark_count: score === null ? 0 : 1,
    success_rate: score === null ? null : 98.5,
    anomaly_rate: score === null ? null : 1.5,
    http_call_count: score === null ? null : 100,
    benchmark_scores: [],
    ...overrides
  };
}

function agent(id: string, name: string, overallScore: number | null, rank: number | null) {
  return {
    agent_id: `agent-${id}`,
    agent_version_id: `version-${id}`,
    agent_name: name,
    provider: "Provider",
    version: 3,
    metrics: {
      overall: metric(overallScore, rank),
      reasoning: metric(overallScore === null ? null : overallScore + 1, rank),
      retrieval: metric(),
      data_reasoning: metric(),
      instruction_following: metric(),
      long_context: metric()
    }
  };
}

function overview(overrides: Record<string, unknown> = {}) {
  const protocol = (metricKey: GlobalLeaderboardMetric) => ({
    metric: metricKey,
    label: metricKey,
    protocol_hash: "abcdef1234567890",
    published_at: "2026-07-14T08:00:00Z",
    benchmark_count: 2,
    question_count: 100,
    ranked_agent_count: 2
  });
  return {
    generated_at: "2026-07-16T08:00:00Z",
    eligible_agent_count: 2,
    protocols: (["overall", "reasoning", "retrieval", "data_reasoning", "instruction_following", "long_context"] as GlobalLeaderboardMetric[]).map(protocol),
    agents: [agent("a", "Agent Alpha", 90, 1), agent("b", "Agent Beta", 85, 2)],
    ...overrides
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter><LeaderboardPage /></MemoryRouter>
    </QueryClientProvider>
  );
}

test("renders a compact podium and the six-metric overview matrix", async () => {
  const fetchMock = vi.fn().mockResolvedValue(response(overview()));
  vi.stubGlobal("fetch", fetchMock);
  const page = renderPage();

  expect(await screen.findByRole("heading", { name: "Agent 排行榜" })).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/v1/leaderboards/global/overview", expect.any(Object));
  expect(page.container.querySelector(".leader-podium article")?.textContent).toContain("Agent Alpha");
  const rows = page.container.querySelectorAll(".ability-overview-board > article");
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveTextContent("Agent Alpha");
  expect(rows[0]).toHaveTextContent("#01");
  expect(rows[0]).toHaveTextContent("90.00");
  expect(screen.getByRole("button", { name: /综合按此列排序/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /指令遵循点击排序/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /长上下文点击排序/ })).toBeInTheDocument();
});

test("switches to a specialist board without fetching a second leaderboard", async () => {
  const alpha = agent("a", "Agent Alpha", 90, 1);
  alpha.metrics.retrieval = metric(72, 1, {
    source_profile_key: "unified-v3-retrieval",
    benchmark_scores: [{
      benchmark_id: "benchmark-1",
      name: "Retrieval Arena",
      ability: "retrieval",
      language: "zh",
      score: 72
    }]
  });
  const fetchMock = vi.fn().mockResolvedValue(response(overview({ agents: [alpha] })));
  vi.stubGlobal("fetch", fetchMock);
  renderPage();

  await screen.findByRole("heading", { name: "Agent 排行榜" });
  fireEvent.click(screen.getByRole("button", { name: /检索榜/ }));

  expect(await screen.findByText("Retrieval Arena")).toBeInTheDocument();
  expect(screen.getAllByText("72.00").length).toBeGreaterThan(0);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("opens the detail drawer and builds a global multi-agent comparison", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(overview())));
  renderPage();

  await screen.findByRole("heading", { name: "Agent 排行榜" });
  fireEvent.click(screen.getByRole("button", { name: /Agent Alpha/ }));
  const detail = screen.getByLabelText("Agent Alpha 评测详情");
  expect(detail).toBeInTheDocument();
  expect(screen.getByText("RESULT SOURCE")).toBeInTheDocument();
  const backdrop = detail.parentElement!;
  fireEvent.mouseDown(backdrop);
  fireEvent.click(backdrop);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.getByLabelText("Agent Alpha 评测详情")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关闭详情" }));

  fireEvent.click(screen.getByRole("checkbox", { name: "选择 Agent Alpha" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "选择 Agent Beta" }));
  expect(screen.getByRole("link", { name: "正式成绩对比" })).toHaveAttribute(
    "href",
    "/leaderboard/compare?agents=agent-a,agent-b"
  );
  expect(screen.getByRole("link", { name: /实时回答对比/ })).toHaveAttribute(
    "href",
    "/compare?agents=agent-a,agent-b"
  );
});

test("keeps entirely unranked agents in a collapsed evaluation section", async () => {
  const pending = agent("pending", "Agent Pending", null, null);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(overview({
    eligible_agent_count: 2,
    agents: [agent("a", "Agent Alpha", 90, 1), pending]
  }))));
  renderPage();

  expect(await screen.findByText("Agent Pending")).toBeInTheDocument();
  expect(screen.getByText("尚未取得任何正式成绩的 Agent")).toBeInTheDocument();
  expect(screen.getAllByText("未评测").length).toBeGreaterThan(0);
  expect(screen.getByRole("link", { name: /前往评测任务/ })).toHaveAttribute("href", "/runs");
});

test("explains an empty specialist metric", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(overview({
    agents: [agent("pending", "Agent Pending", null, null)]
  }))));
  renderPage();

  await screen.findByRole("heading", { name: "Agent 排行榜" });
  fireEvent.click(screen.getByRole("button", { name: /数据推理榜/ }));
  expect(screen.getByRole("heading", { name: "当前视图暂无可排名结果" })).toBeInTheDocument();
  expect(screen.getByText("Agent Pending")).toBeInTheDocument();
});
