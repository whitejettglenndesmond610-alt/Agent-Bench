import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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

test("shows confidence interval and pass metrics when the snapshot provides them", async () => {
  const alpha = agent("a", "Agent Alpha", 90, 1);
  alpha.metrics.overall = metric(90, 1, {
    ci_low: 88,
    ci_high: 92,
    pass_at_1: 96.5,
    pass_at_k: 100,
    pass_pow_k: 81.25
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(overview({ agents: [alpha] }))));
  renderPage();

  await screen.findByRole("heading", { name: "Agent 排行榜" });
  expect(screen.getAllByText("90.00 (88.00–92.00)").length).toBeGreaterThan(0);

  fireEvent.click(screen.getByRole("button", { name: /Agent Alpha/ }));
  const detail = screen.getByLabelText("Agent Alpha 评测详情");
  expect(detail).toHaveTextContent("pass@1 单次成功率");
  expect(detail).toHaveTextContent("96.50%");
  expect(detail).toHaveTextContent("pass^k 多次全对占比");
  expect(detail).toHaveTextContent("81.25%");
});

test("shows cost metrics when the snapshot provides them", async () => {
  const alpha = agent("a", "Agent Alpha", 90, 1);
  alpha.metrics.overall = metric(90, 1, {
    total_tokens: 1500000,
    input_token_share: 66.67,
    output_token_share: 33.33,
    total_cost_usd: 0.1234,
    cost_per_task_usd: 0.0012
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(overview({ agents: [alpha] }))));
  renderPage();

  await screen.findByRole("heading", { name: "Agent 排行榜" });
  fireEvent.click(screen.getByRole("button", { name: /Agent Alpha/ }));
  const detail = screen.getByLabelText("Agent Alpha 评测详情");
  expect(detail).toHaveTextContent("输入/输出 Token");
  expect(detail).toHaveTextContent("66.7% / 33.3%");
  expect(detail).toHaveTextContent("总成本");
  expect(detail).toHaveTextContent("$0.1234");
  expect(detail).toHaveTextContent("成本/题");
  expect(detail).toHaveTextContent("$0.0012");
});

test("shows dashes for cost metrics on legacy snapshots without them", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(overview())));
  renderPage();

  await screen.findByRole("heading", { name: "Agent 排行榜" });
  fireEvent.click(screen.getByRole("button", { name: /Agent Alpha/ }));
  const detail = screen.getByLabelText("Agent Alpha 评测详情");
  expect(detail).toHaveTextContent("总成本");
  expect(detail).toHaveTextContent("成本/题");
  expect(detail).not.toHaveTextContent("$");
});

test("hides confidence interval and pass metrics for legacy snapshots without them", async () => {  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(overview())));
  const page = renderPage();

  await screen.findByRole("heading", { name: "Agent 排行榜" });
  expect(screen.queryByText(/\(/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Agent Alpha/ }));
  const detail = screen.getByLabelText("Agent Alpha 评测详情");
  expect(detail).not.toHaveTextContent("pass@1");
  expect(detail).not.toHaveTextContent("pass^k");
  expect(page.container.textContent).not.toContain("null");
});

test("renders a compact podium and the six-metric overview matrix", async () => {
  const fetchMock = vi.fn().mockResolvedValue(response(overview()));
  vi.stubGlobal("fetch", fetchMock);
  const page = renderPage();

  expect(await screen.findByRole("heading", { name: "Agent 排行榜" })).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/v1/leaderboards/global/overview", expect.any(Object));
  expect(page.container.querySelector(".leader-podium article")?.textContent).toContain("Agent Alpha");
  const rows = page.container.querySelectorAll(".leaderboard-table tbody tr");
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveTextContent("Agent Alpha");
  expect(rows[0]).toHaveTextContent("#01");
  expect(rows[0]).toHaveTextContent("90.00");
  expect(screen.getByRole("button", { name: "按综合排序" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "按指令遵循排序" })).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByRole("button", { name: "按长上下文排序" })).toBeInTheDocument();
});

test("sorts the overview table by clicking ability column headers", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(overview())));
  const page = renderPage();

  await screen.findByRole("heading", { name: "Agent 排行榜" });
  const rows = () => page.container.querySelectorAll(".leaderboard-table tbody tr");
  expect(rows()[0]).toHaveTextContent("Agent Alpha");

  // Default sort is 综合 rank ascending; clicking it again flips to descending.
  const overallSort = screen.getByRole("button", { name: "按综合排序" });
  expect(overallSort).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(overallSort);
  expect(rows()[0]).toHaveTextContent("Agent Beta");

  // Clicking another ability column makes it the sort key, ascending again.
  const reasoningSort = screen.getByRole("button", { name: "按推理排序" });
  expect(reasoningSort).toHaveAttribute("aria-pressed", "false");
  fireEvent.click(reasoningSort);
  expect(reasoningSort).toHaveAttribute("aria-pressed", "true");
  expect(rows()[0]).toHaveTextContent("Agent Alpha");
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
  const retrievalTab = screen.getByRole("button", { name: /检索榜/ });
  fireEvent.click(retrievalTab);

  expect(retrievalTab).toHaveAttribute("aria-pressed", "true");
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
  expect(screen.getByText("当前视图暂无可排名结果")).toBeInTheDocument();
  expect(screen.getByText("Agent Pending")).toBeInTheDocument();
});

test("renders an ability radar chart inside the detail drawer", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(overview())));
  renderPage();

  await screen.findByRole("heading", { name: "Agent 排行榜" });
  fireEvent.click(screen.getByRole("button", { name: /Agent Alpha/ }));

  const detail = screen.getByLabelText("Agent Alpha 评测详情");
  expect(within(detail).getByRole("img")).toBeInTheDocument();
  expect(within(detail).getByText("ABILITY SHAPE")).toBeInTheDocument();
  expect(detail.querySelector(".drawer-radar")).not.toBeNull();
});
