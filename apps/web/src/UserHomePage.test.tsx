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

test("aggregates personal activity, favorites, templates, tasks, and official leaders", async () => {
  const metric = { rank: null, score: null };
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
      agents: [{
        agent_id: "agent-a",
        agent_name: "Agent Alpha",
        provider: "Provider A",
        version: 3,
        metrics: { overall: { rank: 1, score: 93.25 }, reasoning: metric, retrieval: metric, data_reasoning: metric, instruction_following: metric, long_context: metric }
      }]
    }));
    if (input === "/api/v1/workspace/agents/agent-a/favorite" && init?.method === "DELETE") {
      return Promise.resolve(response({}));
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage();

  expect(await screen.findByRole("heading", { name: "小林，今天想让 Agent 做什么？" })).toBeInTheDocument();
  expect(await screen.findByRole("link", { name: /^方案比较/ })).toHaveAttribute("href", "/compare/comparison-1");
  expect(screen.getByRole("link", { name: /^市场调研/ })).toHaveAttribute("href", "/workspace/conversation-1");
  expect(screen.getByRole("link", { name: "用于对话" })).toHaveAttribute("href", "/workspace?template=template-1");
  expect(screen.getByRole("link", { name: "开始体验" })).toHaveAttribute("href", "/compare?task=source-check");
  expect(screen.getByText("93.25")).toBeInTheDocument();
  expect(screen.queryByText("Private Favorite")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "取消收藏 Agent Alpha" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/agents/agent-a/favorite",
    expect.objectContaining({ method: "DELETE" })
  ));
});
