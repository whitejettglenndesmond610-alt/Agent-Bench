import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function response(body: object, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 401 ? "Unauthorized" : "OK",
    json: async () => body
  };
}

function renderApp(initialEntry = "/") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } }
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

test("shows the login form without a session", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({}, 401)));

  renderApp();

  expect(await screen.findByRole("heading", { name: "选择访问身份" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /普通用户/ })).toHaveAttribute(
    "aria-pressed",
    "true"
  );
});

test("shows real evaluation readiness and prioritized operator navigation", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: string) => {
      if (input === "/api/v1/auth/session") {
        return Promise.resolve(
          response({
            role: "operator",
            expires_at: "2026-01-01T08:00:00Z",
            user_id: "shared-operator",
            display_name: "运营访问",
            is_shared: true,
            workspace_enabled: true
          })
        );
      }
       if (input === "/api/v1/dashboard/summary") return Promise.resolve(response({
         active_profile: {
           key: "unified-v4",
           name: "统一综合评测",
           ready: true,
           question_count: 210,
           dataset_version_count: 15,
           language_counts: { zh: 120, en: 90 },
           ability_counts: { reasoning: 50, retrieval: 45, data_reasoning: 40, instruction_following: 35, long_context: 40 },
           ability_weights: { reasoning: 20, retrieval: 20, data_reasoning: 20, instruction_following: 20, long_context: 20 }
         },
         ranking_profiles: [{ key: "unified-v4", name: "统一综合评测", ready: true, question_count: 210 }],
         judge_model: "judge-current"
       }));
       if (input === "/api/v1/evaluation-runs") return Promise.resolve(response([]));
       if (input === "/api/v1/evaluation-runs/agent-statuses") return Promise.resolve(response([]));
       throw new Error(`Unexpected request: ${input}`);
    })
  );

  renderApp();

  expect(await screen.findByText("统一综合评测")).toBeInTheDocument();
  expect(screen.getAllByText("210").length).toBeGreaterThanOrEqual(2);
  expect(screen.getByText("judge-current", { exact: false })).toBeInTheDocument();
  expect(screen.queryByText("服务在线")).not.toBeInTheDocument();
  expect(screen.queryByText("SYSTEM READY")).not.toBeInTheDocument();
  const navigation = screen.getByRole("navigation", { name: "主导航" });
  expect(within(navigation).getByRole("link", { name: "评测总览" })).toBeInTheDocument();
  expect(within(navigation).getByRole("link", { name: "Agent 中心" })).toBeInTheDocument();
  expect(within(navigation).getByRole("link", { name: "在线对话" })).toBeInTheDocument();
  expect(within(navigation).getByRole("link", { name: "首页" })).toBeInTheDocument();
  expect(within(navigation).getByRole("link", { name: "Agent 广场" })).toBeInTheDocument();
  expect(within(navigation).getByRole("link", { name: "评测实验" })).toBeInTheDocument();
  expect(within(navigation).getByRole("link", { name: "评测运行" })).toBeInTheDocument();
  expect(within(navigation).getByRole("link", { name: "案例策展" })).toBeInTheDocument();
  expect(within(navigation).getByRole("link", { name: "排行榜" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "发起评测" })).toHaveAttribute("href", "/runs?launch=1");
});

test("sends a read-only session without workspace access to the global leaderboard", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: string) => {
      if (input === "/api/v1/auth/session") {
        return Promise.resolve(
          response({
            role: "read_only",
            expires_at: "2026-01-01T08:00:00Z",
            user_id: "shared-read-only",
            display_name: "只读访问",
            is_shared: true,
             workspace_enabled: false
          })
        );
      }
      if (input === "/api/v1/leaderboards/global/overview") {
        return Promise.resolve(
          response({
            generated_at: "2026-07-16T08:00:00Z",
            eligible_agent_count: 0,
            protocols: ["overall", "reasoning", "retrieval", "data_reasoning", "instruction_following", "long_context"].map((metric) => ({
              metric,
              label: metric,
              protocol_hash: null,
              published_at: null,
              benchmark_count: 0,
              question_count: 0,
              ranked_agent_count: 0
            })),
            agents: []
          })
        );
      }
      throw new Error(`Unexpected request: ${input}`);
    })
  );

  renderApp();

  expect(await screen.findByRole("heading", { name: "Agent 排行榜" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "评测总览" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Agent 中心" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "在线对话" })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "排行榜" })).toBeInTheDocument();
});

test("uses the named account login endpoint in personal account mode", async () => {
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/auth/session") return Promise.resolve(response({}, 401));
    if (input === "/api/v1/auth/user-login") {
      return Promise.resolve(response({
        role: "read_only",
        expires_at: "2026-07-20T08:00:00Z",
        user_id: "user-1",
        display_name: "测试用户",
        is_shared: false,
        workspace_enabled: true
      }));
    }
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response([]));
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [] }));
    throw new Error(`Unexpected request: ${input} ${init?.method ?? "GET"}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderApp();
  fireEvent.click(await screen.findByRole("button", { name: "个人账号" }));
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "workspace-user" } });
  fireEvent.change(screen.getByLabelText("账号密码"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: /进入平台/ }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/auth/user-login",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ username: "workspace-user", password: "secret" })
    })
  ));
  expect(await screen.findByText("测试用户")).toBeInTheDocument();
  expect(screen.getByText(/个人账号/)).toBeInTheDocument();
});

test("lets an entitled named read-only user open workspace without operator navigation", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/auth/session") {
      return Promise.resolve(response({
        role: "read_only",
        expires_at: "2026-07-20T08:00:00Z",
        user_id: "user-2",
        display_name: "只读研究员",
        is_shared: false,
        workspace_enabled: true
      }));
    }
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response([]));
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [] }));
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderApp("/workspace");

  expect(await screen.findByText("当前账号没有可用的 Agent 版本。")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "在线对话" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "评测总览" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Agent 中心" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "评测运行" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "案例策展" })).not.toBeInTheDocument();
});

test("keeps operator routes guarded and sends entitled users to their product home", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/auth/session") {
      return Promise.resolve(response({
        role: "read_only",
        expires_at: "2026-07-20T08:00:00Z",
        user_id: "user-3",
        display_name: "Workspace Only",
        is_shared: false,
        workspace_enabled: true
      }));
    }
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response([]));
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/workspace/comparisons") return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/workspace/prompt-templates") return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/workspace/experience-tasks") return Promise.resolve(response([]));
    if (input === "/api/v1/leaderboards/global/overview") return Promise.resolve(response({ generated_at: "2026-07-20T08:00:00Z", eligible_agent_count: 0, protocols: [], agents: [] }));
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderApp("/runs");

  expect(await screen.findByRole("heading", { name: /今天想让 Agent 做什么/ })).toBeInTheDocument();
  expect(screen.queryByText("评测任务控制")).not.toBeInTheDocument();
});

test("clears user-scoped workspace cache across logout and account switch", async () => {
  let activeUser = "user-one";
  let resolveUserTwoHistory: ((value: ReturnType<typeof response>) => void) | undefined;
  const userTwoHistory = new Promise<ReturnType<typeof response>>((resolve) => {
    resolveUserTwoHistory = resolve;
  });
  const sessionFor = (id: string, name: string) => ({
    role: "read_only",
    expires_at: "2026-07-20T08:00:00Z",
    user_id: id,
    display_name: name,
    is_shared: false,
    workspace_enabled: true
  });
  const conversationFor = (id: string, title: string) => ({
    id,
    agent_version_id: "version-1",
    title,
    revision: 1,
    last_turn_no: 0,
    archived_at: null,
    created_at: "2026-07-19T08:00:00Z",
    updated_at: "2026-07-19T08:00:00Z",
    agent: {
      agent_id: "agent-1",
      agent_version_id: "version-1",
      name: "Atlas",
      provider: "Atlas",
      version: 1
    }
  });
  const fetchMock = vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/auth/session") {
      return Promise.resolve(response(sessionFor("user-one", "User One")));
    }
    if (input === "/api/v1/auth/logout") return Promise.resolve(response({}));
    if (input === "/api/v1/auth/user-login") {
      activeUser = "user-two";
      return Promise.resolve(response(sessionFor("user-two", "User Two")));
    }
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response([]));
    if (input === "/api/v1/workspace/conversations") {
      return activeUser === "user-one"
        ? Promise.resolve(response({ items: [conversationFor("conversation-one", "User One Secret")] }))
        : userTwoHistory;
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderApp("/workspace");
  expect(await screen.findByText("User One Secret")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "退出" }));

  expect(await screen.findByRole("heading", { name: "选择访问身份" })).toBeInTheDocument();
  expect(screen.queryByText("User One Secret")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "个人账号" }));
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "user-two" } });
  fireEvent.change(screen.getByLabelText("账号密码"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: /进入平台/ }));

  expect(await screen.findByText("User Two")).toBeInTheDocument();
  expect(screen.queryByText("User One Secret")).not.toBeInTheDocument();
  resolveUserTwoHistory?.(response({ items: [conversationFor("conversation-two", "User Two Conversation")] }));
  expect(await screen.findByText("User Two Conversation")).toBeInTheDocument();
  expect(screen.queryByText("User One Secret")).not.toBeInTheDocument();
});
