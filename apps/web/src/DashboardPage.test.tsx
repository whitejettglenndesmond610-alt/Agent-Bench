import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import {
  AgentEvaluationMetric,
  AgentEvaluationStatus,
  DashboardSummary,
  fetchAgentEvaluationStatuses,
  fetchDashboardSummary,
  fetchRuns,
  RunListItem
} from "./api";
import { DashboardPage } from "./DashboardPage";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    fetchDashboardSummary: vi.fn(),
    fetchRuns: vi.fn(),
    fetchAgentEvaluationStatuses: vi.fn()
  };
});

// ECharts needs real layout; swap the canvas for a lightweight stand-in that
// still exposes the aria-label so sparkline wiring stays under test.
vi.mock("./EChart", () => ({
  EChart: ({ ariaLabel }: { ariaLabel: string }) => <div role="img" aria-label={ariaLabel} />
}));

const mockedFetchSummary = vi.mocked(fetchDashboardSummary);
const mockedFetchRuns = vi.mocked(fetchRuns);
const mockedFetchStatuses = vi.mocked(fetchAgentEvaluationStatuses);

function metric(status: AgentEvaluationMetric["status"], updatedAt: string | null = null): AgentEvaluationMetric {
  return { status, score: null, run_id: null, source_version: null, updated_at: updatedAt };
}

function dashboardSummary(): DashboardSummary {
  return {
    active_profile: {
      key: "unified-v3",
      name: "综合基准",
      ready: true,
      question_count: 70,
      dataset_version_count: 2,
      language_counts: { zh: 40, en: 30 },
      ability_counts: { reasoning: 20, retrieval: 15, data_reasoning: 10, instruction_following: 15, long_context: 10, agentic: 0 },
      ability_weights: { reasoning: 20, retrieval: 20, data_reasoning: 20, instruction_following: 20, long_context: 20, agentic: 0 }
    },
    ranking_profiles: [
      { key: "unified-v3", name: "综合基准", ready: true, question_count: 70 },
      { key: "unified-v3-reasoning", name: "推理专项", ready: false, question_count: 40 }
    ],
    judge_model: "deepseek-chat"
  };
}

function run(partial: Partial<RunListItem> & Pick<RunListItem, "id" | "name" | "status">): RunListItem {
  return {
    desired_state: "running",
    stage: "inference",
    profile_key: "unified-v3",
    run_scope: "comprehensive",
    target_ability: null,
    ranking_eligible: true,
    trial_count: 1,
    heartbeat_at: null,
    started_at: null,
    finished_at: null,
    system_error: null,
    created_at: "2026-07-16T08:00:00Z",
    agent_count: 2,
    total: 40,
    completed: 0,
    failed: 0,
    retried: 0,
    ...partial
  };
}

function agentStatus(): AgentEvaluationStatus {
  return {
    agent_id: "agent-1",
    agent_version_id: "version-1",
    agent_name: "Agent Alpha",
    provider: "Provider A",
    version: 1,
    max_prompt_characters: null,
    supports_citations: true,
    comprehensive: metric("evaluated", "2026-07-15T10:00:00Z"),
    reasoning: metric("evaluated", "2026-07-14T10:00:00Z"),
    retrieval: metric("evaluated", "2026-07-13T10:00:00Z"),
    data_reasoning: metric("not_evaluated"),
    instruction_following: metric("not_evaluated"),
    long_context: metric("not_evaluated")
  };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } }
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  mockedFetchSummary.mockResolvedValue(dashboardSummary());
  mockedFetchRuns.mockResolvedValue([
    run({ id: "run-1", name: "综合回归评测", status: "running", completed: 14, failed: 1 }),
    run({ id: "run-2", name: "历史发布评测", status: "published", completed: 40, created_at: "2026-07-10T08:00:00Z" }),
    run({ id: "run-3", name: "排队回归评测", status: "queued", created_at: "2026-07-15T08:00:00Z" }),
    run({ id: "run-4", name: "早期发布评测", status: "published", completed: 40, created_at: "2026-07-08T08:00:00Z" })
  ]);
  mockedFetchStatuses.mockResolvedValue([agentStatus()]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

test("renders the page header with profile meta and primary actions", async () => {
  renderPage();

  expect(await screen.findByRole("heading", { name: "评测总览" })).toBeInTheDocument();
  expect(screen.getByText("综合基准")).toBeInTheDocument();
  expect(screen.getByText("unified-v3 · 70 题 · 2 个数据集版本")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "发起评测" })).toHaveAttribute("href", "/runs?launch=1");
  expect(screen.getByRole("link", { name: "在线运行 Agent" })).toHaveAttribute("href", "/workspace");
});

test("renders KPI cards with counted values and derived sparklines", async () => {
  renderPage();

  await waitFor(() => expect(screen.getByText("50%")).toBeInTheDocument(), { timeout: 2500 });
  await waitFor(() => expect(screen.getAllByText("70").length).toBeGreaterThanOrEqual(1), { timeout: 2500 });
  // 执行中 2（运行 + 排队）/ 已发布 2：两张小卡片数字都是 2
  await waitFor(() => expect(screen.getAllByText("2").length).toBeGreaterThanOrEqual(2), { timeout: 2500 });
  expect(screen.getByText("3 / 6 项有效")).toBeInTheDocument();

  expect(screen.getByRole("img", { name: "各能力维度的题目数量分布" })).toBeInTheDocument();
  expect(screen.getByRole("img", { name: "执行中运行随时间的累计数量" })).toBeInTheDocument();
  expect(screen.getByRole("img", { name: "已发布运行随时间的累计数量" })).toBeInTheDocument();
  expect(screen.getByRole("img", { name: "已评测能力项随时间的累计数量" })).toBeInTheDocument();
});

test("lists recent runs with status pills and progress bars", async () => {
  renderPage();

  const runLink = await screen.findByRole("link", { name: /综合回归评测/ });
  expect(runLink).toHaveAttribute("href", "/runs/run-1");
  expect(screen.getByText("运行中")).toBeInTheDocument();
  expect(screen.getByText("排队中")).toBeInTheDocument();
  expect(screen.getAllByText("已发布").length).toBe(2);
  // (14 + 1) / 40 ≈ 38%
  expect(screen.getByText("38%")).toBeInTheDocument();
  const progress = screen.getByRole("progressbar", { name: "综合回归评测 完成进度" });
  expect(progress).toHaveAttribute("aria-valuenow", "38");
});

test("lists agent evaluation readiness", async () => {
  renderPage();

  const agentLink = await screen.findByRole("link", { name: /Agent Alpha/ });
  expect(agentLink).toHaveAttribute("href", "/agents/agent-1");
  expect(screen.getByText("3 / 6 已评测")).toBeInTheDocument();
});

test("shows empty states when there are no runs or agents", async () => {
  mockedFetchRuns.mockResolvedValue([]);
  mockedFetchStatuses.mockResolvedValue([]);
  renderPage();

  expect(await screen.findByText("尚无评测运行")).toBeInTheDocument();
  expect(screen.getByText("发起一次评测以建立第一条能力记录。")).toBeInTheDocument();
  expect(screen.getByText("尚无可评测 Agent")).toBeInTheDocument();
  // 没有运行数据时不渲染运行趋势图
  expect(screen.queryByRole("img", { name: "执行中运行随时间的累计数量" })).not.toBeInTheDocument();
});
