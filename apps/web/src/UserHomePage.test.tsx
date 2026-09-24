import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { UserHomePage } from "./UserHomePage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function response(body: object) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UserHomePage session={{
          role: "read_only",
          expires_at: "2026-07-21T00:00:00Z",
          user_id: "user-1",
          display_name: "小林",
          is_shared: false,
          workspace_enabled: true
        }} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const metric = { rank: null, score: null };

function overviewAgent(overrides: Record<string, unknown> = {}) {
  return {
    agent_id: "agent-a",
    agent_name: "Agent Alpha",
    provider: "Provider A",
    version: 3,
    metrics: { overall: { rank: 1, score: 93.25 }, reasoning: metric, retrieval: metric, data_reasoning: metric, instruction_following: metric, long_context: metric },
    ...overrides
  };
}

/** 默认空响应：所有数据源返回空集合，动态/历史类端点返回空列表。 */
function emptyFetchMock() {
  return vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/workspace/comparisons") return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response([]));
    if (input === "/api/v1/workspace/prompt-templates") return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/workspace/experience-tasks") return Promise.resolve(response([]));
    if (input === "/api/v1/leaderboards/global/overview") return Promise.resolve(response({ agents: [] }));
    if (input === "/api/v1/leaderboards/history") return Promise.resolve(response([]));
    if (input === "/api/v1/evaluation-runs") return Promise.resolve(response([]));
    if (input === "/api/v1/quality/clusters") return Promise.resolve(response([]));
    if (input === "/api/v1/dataset-releases") return Promise.resolve(response({ items: [] }));
    throw new Error(`Unexpected request: ${input}`);
  });
}

test("aggregates personal activity, favorites, templates, tasks, and official leaders", async () => {
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [{
      id: "conversation-1",
      title: "市场调研",
      last_turn_no: 2,
      archived_at: null,
      updated_at: "2026-07-19T08:00:00Z",
      agent: { name: "Agent Alpha" }
    }] }));
    if (input === "/api/v1/workspace/comparisons") return Promise.resolve(response({ items: [{
      id: "comparison-1",
      title: "方案比较",
      status: "completed",
      counts: { succeeded: 2 },
      archived_at: null,
      updated_at: "2026-07-20T08:00:00Z",
      items: [{ agent: { name: "Agent Alpha" } }, { agent: { name: "Agent Beta" } }]
    }] }));
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response([{
      agent_id: "agent-a",
      agent_version_id: "version-a",
      name: "Agent Alpha",
      provider: "Provider A",
      description: "研究助手",
      logo_url: null,
      version: 3,
      max_prompt_characters: 10000,
      supports_citations: true,
      is_favorite: true,
      visibility: "public",
      model: null
    }, {
      agent_id: "private-a",
      agent_version_id: "private-version-a",
      name: "Private Favorite",
      provider: "Private Provider",
      description: "不应出现在首页收藏",
      logo_url: null,
      version: 1,
      max_prompt_characters: null,
      supports_citations: false,
      is_favorite: true,
      visibility: "private",
      model: "private-model"
    }]));
    if (input === "/api/v1/workspace/prompt-templates") return Promise.resolve(response({ items: [{
      id: "template-1",
      name: "会议总结",
      content: "总结以下会议内容",
      revision: 1,
      created_at: "2026-07-18T08:00:00Z",
      updated_at: "2026-07-18T08:00:00Z"
    }] }));
    if (input === "/api/v1/workspace/experience-tasks") return Promise.resolve(response([{
      slug: "source-check",
      title: "核验公开来源",
      summary: "比较多个公开来源。",
      prompt: "请核验来源",
      category: "retrieval_citations",
      difficulty: "intermediate",
      recommended_mode: "comparison",
      min_agents: 2,
      max_agents: 3,
      ranking_eligible: false
    }]));
    if (input === "/api/v1/leaderboards/global/overview") return Promise.resolve(response({
      agents: [overviewAgent()]
    }));
    if (input === "/api/v1/workspace/agents/agent-a/favorite" && init?.method === "DELETE") {
      return Promise.resolve(response({}));
    }
    if (input === "/api/v1/leaderboards/history") return Promise.resolve(response([]));
    if (input === "/api/v1/evaluation-runs") return Promise.resolve(response([]));
    if (input === "/api/v1/quality/clusters") return Promise.resolve(response([]));
    if (input === "/api/v1/dataset-releases") return Promise.resolve(response({ items: [] }));
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage();

  expect(await screen.findByRole("heading", { name: "小林，今天想让 Agent 做什么？" })).toBeInTheDocument();
  expect(await screen.findByRole("link", { name: /方案比较/ })).toHaveAttribute("href", "/compare/comparison-1");
  expect(screen.getByRole("link", { name: /市场调研/ })).toHaveAttribute("href", "/workspace/conversation-1");
  expect(screen.getByRole("link", { name: "用于对话" })).toHaveAttribute("href", "/workspace?template=template-1");
  expect(screen.getByRole("link", { name: "开始体验" })).toHaveAttribute("href", "/compare?task=source-check");
  expect(screen.getAllByText("93.25").length).toBeGreaterThan(0);
  expect(screen.queryByText("Private Favorite")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "取消收藏 Agent Alpha" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/agents/agent-a/favorite",
    expect.objectContaining({ method: "DELETE" })
  ));
});

test("hero renders the latest comparison answers with excerpts and latency", async () => {
  const answer = "结论是三季度复购率上升了 4.2 个百分点，主要来自会员体系改版与定向券投放，建议延续该策略并观察一个月。";
  const fetchMock = emptyFetchMock().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/comparisons") return Promise.resolve(response({ items: [{
      id: "comparison-hero",
      title: "复购分析",
      prompt: "分析三季度复购率变化原因",
      status: "completed",
      counts: { queued: 0, running: 0, succeeded: 2, failed: 0, canceled: 0 },
      archived_at: null,
      updated_at: "2026-07-20T08:00:00Z",
      items: [
        {
          position: 1,
          conversation_id: "conversation-1",
          agent: { name: "Agent Alpha", provider: "Provider A" },
          attempt: { status: "succeeded", latency_ms: 2340, assistant_message: { content: answer } }
        },
        {
          position: 2,
          conversation_id: "conversation-2",
          agent: { name: "Agent Beta", provider: "Provider B" },
          attempt: { status: "succeeded", latency_ms: 4100, assistant_message: { content: "复购率上升与会员改版强相关。" } }
        }
      ]
    }] }));
    return emptyFetchMock()(input);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage();

  expect(await screen.findByText(/三季度复购率上升了 4\.2 个百分点/)).toBeInTheDocument();
  expect(screen.getByText("分析三季度复购率变化原因")).toBeInTheDocument();
  expect(screen.getByText("Agent Beta")).toBeInTheDocument();
  expect(screen.getByText("2.3s")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "完整对比" })).toHaveAttribute("href", "/compare/comparison-hero");
  // 无榜首数据时 hero 不显示引导卡（已有真实对比内容）。
  expect(screen.queryByRole("link", { name: "发起第一次对比" })).not.toBeInTheDocument();
});

test("hero degrades to a typographic guide when there is no comparison and no leader", async () => {
  vi.stubGlobal("fetch", emptyFetchMock());
  renderPage();

  expect(await screen.findByRole("link", { name: "发起第一次对比" })).toHaveAttribute("href", "/compare");
  expect(screen.getByText(/同一个问题/)).toBeInTheDocument();
});

test("leader podium shows delta against the previous snapshot and hides it without history", async () => {
  const history = [
    { id: "snapshot-current", run_id: "run-2", run_name: "七月复测", published_at: "2026-07-20T00:00:00Z", agent_count: 3, is_current: true },
    { id: "snapshot-previous", run_id: "run-1", run_name: "六月首测", published_at: "2026-06-20T00:00:00Z", agent_count: 3, is_current: false }
  ];
  const fetchMock = emptyFetchMock().mockImplementation((input: string) => {
    if (input === "/api/v1/leaderboards/global/overview") return Promise.resolve(response({ agents: [overviewAgent()] }));
    if (input === "/api/v1/leaderboards/history") return Promise.resolve(response(history));
    if (input === "/api/v1/leaderboards/snapshot-previous") return Promise.resolve(response({
      id: "snapshot-previous",
      run_id: "run-1",
      run_name: "六月首测",
      dataset_bundle_hash: "hash",
      published_at: "2026-06-20T00:00:00Z",
      rankings: [{
        rank: 2,
        run_agent_id: "run-agent-a",
        agent_id: "agent-a",
        agent_version_id: "version-a-2",
        agent_name: "Agent Alpha",
        provider: "Provider A",
        version: 2,
        overall_score: 91.05,
        ability_scores: {},
        language_scores: { zh: 91, en: 91 },
        benchmark_scores: [],
        answer_quality: 90,
        citation_quality: 90,
        success_rate: 99,
        anomaly_rate: 0,
        http_call_count: 10
      }]
    }));
    return emptyFetchMock()(input);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage();

  // 名次 2 → 1 上升 1 名，分数 91.05 → 93.25 = +2.20。
  const deltas = await screen.findAllByLabelText(/较上一发布快照/);
  expect(deltas.length).toBeGreaterThan(0);
  expect(deltas[0]).toHaveTextContent("↑1");
  expect(deltas[0]).toHaveTextContent("+2.20");
});

test("leader podium hides the delta mark when history is unavailable", async () => {
  const fetchMock = emptyFetchMock().mockImplementation((input: string) => {
    if (input === "/api/v1/leaderboards/global/overview") return Promise.resolve(response({ agents: [overviewAgent()] }));
    return emptyFetchMock()(input);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage();

  expect(await screen.findAllByText("93.25")).not.toHaveLength(0);
  expect(screen.queryByLabelText(/较上一发布快照/)).not.toBeInTheDocument();
});

test("platform pulse lists real events and disappears when every source is empty", async () => {
  const withData = emptyFetchMock().mockImplementation((input: string) => {
    if (input === "/api/v1/evaluation-runs") return Promise.resolve(response([{
      id: "run-9",
      name: "七月正式复测",
      status: "published",
      desired_state: "running",
      stage: "ready_to_publish",
      profile_key: "unified-v3",
      run_scope: "comprehensive",
      target_ability: null,
      ranking_eligible: true,
      trial_count: 1,
      heartbeat_at: null,
      started_at: null,
      finished_at: "2026-07-20T06:00:00Z",
      system_error: null,
      created_at: "2026-07-19T06:00:00Z",
      agent_count: 3,
      total: 90,
      completed: 90,
      failed: 0,
      retried: 0
    }]));
    if (input === "/api/v1/quality/clusters") return Promise.resolve(response([{
      id: "cluster-1",
      run_id: null,
      run_name: null,
      window_days: 7,
      name: "超时集中",
      description: null,
      sample_count: 4,
      failure_count: 12,
      samples: [],
      created_at: "2026-07-20T05:00:00Z"
    }]));
    if (input === "/api/v1/dataset-releases") return Promise.resolve(response({ items: [{
      id: "release-1",
      name: "正式题库 v3",
      description: null,
      status: "published",
      revision: 3,
      manifest_hash: "hash",
      approved_by_user_id: null,
      approved_at: null,
      published_by_user_id: null,
      published_at: "2026-07-18T05:00:00Z",
      created_at: "2026-07-17T05:00:00Z",
      updated_at: "2026-07-18T05:00:00Z",
      cases: [],
      dataset_versions: []
    }] }));
    return emptyFetchMock()(input);
  });
  vi.stubGlobal("fetch", withData);
  const { unmount } = renderPage();

  expect(await screen.findByText(/评测运行「七月正式复测」完成发布/)).toHaveAttribute("href", "/runs/run-9");
  expect(screen.getByText(/新失败聚类「超时集中」/)).toHaveAttribute("href", "/quality");
  expect(screen.getByText(/数据集「正式题库 v3」R3 正式发布/)).toHaveAttribute("href", "/dataset-releases/release-1");
  unmount();

  cleanup();
  vi.stubGlobal("fetch", emptyFetchMock());
  renderPage();
  await screen.findByRole("heading", { name: "小林，今天想让 Agent 做什么？" });
  await waitFor(() => expect(screen.queryByLabelText("平台动态")).not.toBeInTheDocument());
});
