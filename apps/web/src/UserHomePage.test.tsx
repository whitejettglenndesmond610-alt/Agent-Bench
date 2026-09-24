import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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

/** 默认空响应：首页只依赖会话/对比/榜单三个数据源，全部返回空集合。 */
function emptyFetchMock() {
  return vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/workspace/comparisons") return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/leaderboards/global/overview") return Promise.resolve(response({ agents: [] }));
    if (input === "/api/v1/leaderboards/history") return Promise.resolve(response([]));
    throw new Error(`Unexpected request: ${input}`);
  });
}

test("renders the greeting and three action cards with navigation targets", async () => {
  vi.stubGlobal("fetch", emptyFetchMock());
  renderPage();

  expect(await screen.findByRole("heading", { name: "小林，今天想让 Agent 做什么？" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /开始在线对话/ })).toHaveAttribute("href", "/workspace");
  expect(screen.getByRole("link", { name: /发起多 Agent 对比/ })).toHaveAttribute("href", "/compare");
  expect(screen.getByRole("link", { name: /浏览 Agent 广场/ })).toHaveAttribute("href", "/plaza");
});

test("content band lists recent records and the current leader", async () => {
  const fetchMock = emptyFetchMock().mockImplementation((input: string) => {
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
    if (input === "/api/v1/leaderboards/global/overview") return Promise.resolve(response({
      agents: [overviewAgent()]
    }));
    return emptyFetchMock()(input);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage();

  expect(await screen.findByRole("link", { name: /方案比较/ })).toHaveAttribute("href", "/compare/comparison-1");
  expect(screen.getByRole("link", { name: /市场调研/ })).toHaveAttribute("href", "/workspace/conversation-1");
  expect(screen.getByRole("link", { name: "查看全部" })).toHaveAttribute("href", "/records");
  const leaderCard = screen.getByRole("link", { name: /Agent Alpha/ });
  expect(leaderCard).toHaveAttribute("href", "/leaderboard");
  expect(leaderCard).toHaveTextContent("93.25");
  expect(leaderCard).toHaveTextContent("当前榜首");
});

test("leader strip shows delta against the previous snapshot and hides it without history", async () => {
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
  const delta = await screen.findByLabelText(/较上一发布快照/);
  expect(delta).toHaveTextContent("↑1");
  expect(delta).toHaveTextContent("+2.20");
});

test("leader strip hides the delta mark when history is unavailable", async () => {
  const fetchMock = emptyFetchMock().mockImplementation((input: string) => {
    if (input === "/api/v1/leaderboards/global/overview") return Promise.resolve(response({ agents: [overviewAgent()] }));
    return emptyFetchMock()(input);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage();

  expect(await screen.findByText("93.25")).toBeInTheDocument();
  expect(screen.queryByLabelText(/较上一发布快照/)).not.toBeInTheDocument();
});

test("empty data state keeps the hero and drops the whole content band", async () => {
  vi.stubGlobal("fetch", emptyFetchMock());
  renderPage();

  expect(await screen.findByRole("heading", { name: "小林，今天想让 Agent 做什么？" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /开始在线对话/ })).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByLabelText("动态一览")).not.toBeInTheDocument());
  expect(screen.queryByRole("link", { name: "查看全部" })).not.toBeInTheDocument();
  expect(screen.queryByText(/当前榜首/)).not.toBeInTheDocument();
});
