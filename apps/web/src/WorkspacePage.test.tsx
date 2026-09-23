import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { WorkspacePage } from "./WorkspacePage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.cookie = "agent_eval_csrf=; Max-Age=0; path=/";
});

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? "Request failed" : "OK",
    json: async () => body
  };
}

const agent = {
  agent_id: "agent-1",
  agent_version_id: "version-1",
  name: "Atlas Agent",
  provider: "Atlas",
  description: "带引用的研究助手",
  logo_url: null,
  version: 3,
  max_prompt_characters: 4000,
  supports_citations: true,
  visibility: "public" as const,
  model: null
};

const conversation = {
  id: "conversation-1",
  agent_version_id: "version-1",
  title: "研发问答",
  revision: 2,
  last_turn_no: 0,
  archived_at: null,
  created_at: "2026-07-19T08:00:00Z",
  updated_at: "2026-07-19T08:00:00Z",
  agent: {
    agent_id: agent.agent_id,
    agent_version_id: agent.agent_version_id,
    name: agent.name,
    provider: agent.provider,
    version: agent.version,
    visibility: "public" as const,
    model: null
  }
};

function userMessage(turnNo: number, content = `问题 ${turnNo}`) {
  return {
    id: `user-${turnNo}`,
    turn_no: turnNo,
    role: "user",
    variant_no: 1,
    active: true,
    content,
    citations: [],
    created_at: "2026-07-19T08:00:00Z"
  };
}

function assistantMessage(id: string, variantNo: number, content: string, active = true) {
  return {
    id,
    turn_no: 1,
    role: "assistant",
    variant_no: variantNo,
    active,
    content,
    citations: [],
    created_at: "2026-07-19T08:00:01Z"
  };
}

function attempt(
  id: string,
  status: "queued" | "running" | "succeeded" | "failed" | "canceled",
  generationNo = 1,
  assistantMessageId: string | null = null,
  cancelRequestedAt: string | null = null
) {
  return {
    id,
    conversation_id: conversation.id,
    user_message_id: "user-1",
    generation_no: generationNo,
    status,
    assistant_message_id: assistantMessageId,
    agent_version_id: agent.agent_version_id,
    error_category: status === "failed" ? "provider" : null,
    assistant_message: null,
    http_attempt_count: 1,
    last_http_status: null,
    latency_ms: status === "succeeded" ? 840 : null,
    input_tokens: status === "succeeded" ? 12 : null,
    output_tokens: status === "succeeded" ? 24 : null,
    total_tokens: status === "succeeded" ? 36 : null,
    error: status === "failed" ? { type: "upstream_error", category: "provider" } : null,
    cancel_requested_at: cancelRequestedAt,
    started_at: "2026-07-19T08:00:00Z",
    finished_at: status === "succeeded" || status === "failed" ? "2026-07-19T08:00:01Z" : null,
    created_at: "2026-07-19T08:00:00Z",
    updated_at: "2026-07-19T08:00:01Z"
  };
}

function renderWorkspace(initialEntry = "/workspace", canDraft = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } }
  });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/workspace" element={<WorkspacePage userId="user-1" canDraft={canDraft} />} />
          <Route path="/workspace/:conversationId" element={<WorkspacePage userId="user-1" canDraft={canDraft} />} />
          <Route path="/evaluation-cases/:caseId" element={<div>案例编辑器占位</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return Object.assign(rendered, { queryClient });
}

function baseFetch(input: string) {
  if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([agent]));
  if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [conversation] }));
  if (input === "/api/v1/evaluation-cases") return Promise.resolve(response({ items: [] }));
  throw new Error(`Unexpected request: ${input}`);
}

test("loads the catalog and history and creates a persisted empty conversation", async () => {
  document.cookie = "agent_eval_csrf=csrf-token; path=/";
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([agent]));
    if (input === "/api/v1/workspace/conversations" && !init?.method) {
      return Promise.resolve(response({ items: [conversation] }));
    }
    if (input === "/api/v1/workspace/conversations" && init?.method === "POST") {
      return Promise.resolve(response({ ...conversation, revision: 1, last_turn_no: 0 }));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      return Promise.resolve(response({ ...conversation, revision: 1, turns: [] }));
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace();

  expect(await screen.findByText("带引用的研究助手")).toBeInTheDocument();
  expect(screen.getByText("研发问答")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "建立空会话" }));

  expect(await screen.findByText("会话已建立，等待第一条消息。")).toBeInTheDocument();
  const createCall = fetchMock.mock.calls.find((call) => call[0] === "/api/v1/workspace/conversations" && call[1]?.method === "POST");
  expect(createCall?.[1]).toEqual(expect.objectContaining({
    body: JSON.stringify({ agent_version_id: "version-1" }),
    headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" })
  }));
});

test("preselects only an available Agent version from the query parameter", async () => {
  const betaAgent = {
    ...agent,
    agent_id: "agent-2",
    agent_version_id: "version-2",
    name: "Beta Agent",
    provider: "Beta",
    description: "Query selected Agent",
    version: 5,
    visibility: "private" as const,
    model: "deepseek-chat"
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([agent, betaAgent]));
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [] }));
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderWorkspace("/workspace?agentVersion=version-2", false);

  expect(await screen.findByText("Query selected Agent")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "选择 Agent" })).toHaveTextContent("Beta Agent");
  expect(screen.getByRole("button", { name: "选择 Agent" })).toHaveTextContent("私人 API");
  expect(screen.getByRole("button", { name: "选择 Agent" })).toHaveTextContent("deepseek-chat");
  expect(screen.queryByText("带引用的研究助手")).not.toBeInTheDocument();
});

test("ignores an unavailable Agent version query and preserves the default selection", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([agent]));
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [] }));
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderWorkspace("/workspace?agentVersion=missing-version", false);

  expect(await screen.findByText("带引用的研究助手")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "选择 Agent" })).toHaveTextContent("Atlas Agent");
});

test("uses Enter to lazily create and send once while Shift+Enter remains a newline command", async () => {
  document.cookie = "agent_eval_csrf=csrf-token; path=/";
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000001");
  let created = false;
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([agent]));
    if (input === "/api/v1/workspace/conversations" && !init?.method) {
      return Promise.resolve(response({ items: [] }));
    }
    if (input === "/api/v1/workspace/conversations" && init?.method === "POST") {
      created = true;
      return Promise.resolve(response({ ...conversation, revision: 1 }));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1/messages") {
      return Promise.resolve(response({
        conversation_revision: 2,
        user_message: userMessage(1, "分析这份数据"),
        attempt: attempt("attempt-1", "queued")
      }, 202));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      return Promise.resolve(response({ ...conversation, turns: [] }));
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace();
  const textbox = await screen.findByRole("textbox", { name: "发送消息" });
  fireEvent.change(textbox, { target: { value: "分析这份数据" } });
  fireEvent.keyDown(textbox, { key: "Enter", shiftKey: true });
  expect(created).toBe(false);

  fireEvent.keyDown(textbox, { key: "Enter" });
  fireEvent.keyDown(textbox, { key: "Enter" });

  await waitFor(() => expect(fetchMock.mock.calls.filter((call) => call[0] === "/api/v1/workspace/conversations/conversation-1/messages")).toHaveLength(1));
  const sendCall = fetchMock.mock.calls.find((call) => call[0] === "/api/v1/workspace/conversations/conversation-1/messages");
  expect(JSON.parse(String(sendCall?.[1]?.body))).toEqual({
    content: "分析这份数据",
    idempotency_key: "00000000-0000-4000-8000-000000000001",
    expected_revision: 1
  });
});

test("never creates or sends while a routed conversation detail is pending or missing", async () => {
  let resolveDetail: ((value: ReturnType<typeof response>) => void) | undefined;
  const detailPromise = new Promise<ReturnType<typeof response>>((resolve) => {
    resolveDetail = resolve;
  });
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/conversations/conversation-missing") return detailPromise;
    if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([agent]));
    if (input === "/api/v1/workspace/conversations" && !init?.method) {
      return Promise.resolve(response({ items: [] }));
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace("/workspace/conversation-missing");
  const textbox = screen.getByRole("textbox", { name: "发送消息" });
  expect(textbox).toBeDisabled();
  fireEvent.keyDown(textbox, { key: "Enter" });
  expect(fetchMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(false);

  resolveDetail?.(response({ detail: { code: "conversation_not_found", message: "Conversation not found" } }, 404));
  expect(await screen.findByText("Conversation not found")).toBeInTheDocument();
  fireEvent.keyDown(textbox, { key: "Enter" });
  expect(fetchMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(false);
  expect(screen.queryByText("研发问答")).not.toBeInTheDocument();
});

test("reconciles a sent user turn and queued attempt before the detail refetch completes", async () => {
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000011");
  let detailCalls = 0;
  const neverCompletes = new Promise(() => undefined);
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/conversations/conversation-1" && !init?.method) {
      detailCalls += 1;
      if (detailCalls > 1) return neverCompletes;
      return Promise.resolve(response({ ...conversation, turns: [] }));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1/messages") {
      return Promise.resolve(response({
        conversation_revision: 3,
        user_message: userMessage(1, "立即进入缓存"),
        attempt: attempt("attempt-cache", "queued")
      }, 202));
    }
    return baseFetch(input);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace("/workspace/conversation-1");
  const textbox = await screen.findByRole("textbox", { name: "发送消息" });
  fireEvent.change(textbox, { target: { value: "立即进入缓存" } });
  fireEvent.keyDown(textbox, { key: "Enter" });

  expect(await screen.findByText("立即进入缓存")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("等待调度");
  expect(screen.getByRole("button", { name: /生成中/ })).toBeDisabled();
});

test("blocks stale sending during a revision-conflict refetch", async () => {
  let detailCalls = 0;
  let resolveRefetch: ((value: ReturnType<typeof response>) => void) | undefined;
  const refetchPromise = new Promise<ReturnType<typeof response>>((resolve) => {
    resolveRefetch = resolve;
  });
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/conversations/conversation-1" && !init?.method) {
      detailCalls += 1;
      return detailCalls === 1
        ? Promise.resolve(response({ ...conversation, turns: [] }))
        : refetchPromise;
    }
    if (input === "/api/v1/workspace/conversations/conversation-1/messages") {
      return Promise.resolve(response({
        detail: { code: "revision_conflict", message: "Conversation revision changed" }
      }, 409));
    }
    return baseFetch(input);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace("/workspace/conversation-1");
  const textbox = await screen.findByRole("textbox", { name: "发送消息" });
  fireEvent.change(textbox, { target: { value: "冲突消息" } });
  fireEvent.keyDown(textbox, { key: "Enter" });

  expect(await screen.findByRole("alert")).toHaveTextContent("正在同步");
  expect(textbox).toBeDisabled();
  resolveRefetch?.(response({ ...conversation, revision: 3, turns: [] }));
  await waitFor(() => expect(textbox).toBeEnabled());
  expect(textbox).toHaveValue("冲突消息");
});

test("shows the server prompt-limit error without claiming an exact cumulative count", async () => {
  const fetchMock = vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      return Promise.resolve(response({ ...conversation, turns: [] }));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1/messages") {
      return Promise.resolve(response({
        detail: { code: "prompt_too_long", message: "Conversation exceeds the Agent prompt limit" }
      }, 422));
    }
    return baseFetch(input);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace("/workspace/conversation-1");
  const textbox = await screen.findByRole("textbox", { name: "发送消息" });
  fireEvent.change(textbox, { target: { value: "仍由服务器计算上下文" } });
  fireEvent.keyDown(textbox, { key: "Enter" });

  expect(await screen.findByRole("alert")).toHaveTextContent("完整上下文超过");
  expect(screen.getByText(/完整上下文由服务器校验/)).toBeInTheDocument();
  expect(screen.queryByText(/对话累计/)).not.toBeInTheDocument();
});

test("polls a queued attempt until the persisted answer succeeds", async () => {
  let detailCalls = 0;
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      detailCalls += 1;
      const pendingTurn = {
        turn_no: 1,
        user_message: userMessage(1),
        assistant_messages: [],
        attempts: [attempt("attempt-1", "queued")]
      };
      const succeededTurn = {
        ...pendingTurn,
        assistant_messages: [assistantMessage("answer-1", 1, "轮询完成的回答")],
        attempts: [attempt("attempt-1", "succeeded", 1, "answer-1")]
      };
      return Promise.resolve(response({
        ...conversation,
        last_turn_no: 1,
        turns: [detailCalls === 1 ? pendingTurn : succeededTurn]
      }));
    }
    return baseFetch(input);
  }));

  renderWorkspace("/workspace/conversation-1");

  expect(await screen.findByRole("status")).toHaveTextContent("等待调度");
  expect(await screen.findByText("轮询完成的回答", {}, { timeout: 2500 })).toBeInTheDocument();
  expect(screen.getByText(/840 MS/)).toBeInTheDocument();
  expect(screen.getByText(/TOKEN 36/)).toBeInTheDocument();
  expect(detailCalls).toBeGreaterThanOrEqual(2);
});

test("cancels an active attempt through the server endpoint", async () => {
  document.cookie = "agent_eval_csrf=csrf-token; path=/";
  let liveAttempt = attempt("attempt-live", "running");
  const queuedTurn = () => ({
    turn_no: 1,
    user_message: userMessage(1),
    assistant_messages: [],
    attempts: [liveAttempt]
  });
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      return Promise.resolve(response({ ...conversation, last_turn_no: 1, turns: [queuedTurn()] }));
    }
    if (input === "/api/v1/workspace/attempts/attempt-live/cancel" && init?.method === "POST") {
      liveAttempt = attempt("attempt-live", "running", 1, null, "2026-07-19T08:00:02Z");
      return Promise.resolve(response(liveAttempt));
    }
    return baseFetch(input);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace("/workspace/conversation-1");
  fireEvent.click(await screen.findByRole("button", { name: "停止" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/attempts/attempt-live/cancel",
    expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" })
    })
  ));
  expect((await screen.findAllByText("正在停止")).length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "停止中" })).toBeDisabled();
});

test("shows a categorized failure and retries only the latest turn", async () => {
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000002");
  const failedTurn = {
    turn_no: 1,
    user_message: userMessage(1),
    assistant_messages: [],
    attempts: [attempt("attempt-failed", "failed")]
  };
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      return Promise.resolve(response({ ...conversation, last_turn_no: 1, turns: [failedTurn] }));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1/messages/user-1/regenerate" && init?.method === "POST") {
      return Promise.resolve(response({ conversation_revision: 3, attempt: attempt("attempt-retry", "queued", 2) }, 202));
    }
    return baseFetch(input);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace("/workspace/conversation-1");

  expect(await screen.findByText(/错误分类 provider/)).toHaveTextContent("upstream_error");
  fireEvent.click(screen.getByRole("button", { name: "重试生成" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/conversations/conversation-1/messages/user-1/regenerate",
    expect.objectContaining({
      body: JSON.stringify({
        idempotency_key: "00000000-0000-4000-8000-000000000002",
        expected_revision: 2
      })
    })
  ));
});

test("offers regenerate after the first generation is canceled without an answer", async () => {
  let detailCalls = 0;
  const pendingRefetch = new Promise(() => undefined);
  const canceledTurn = {
    turn_no: 1,
    user_message: userMessage(1),
    assistant_messages: [],
    attempts: [attempt("attempt-canceled", "canceled")]
  };
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      detailCalls += 1;
      return detailCalls === 1
        ? Promise.resolve(response({ ...conversation, last_turn_no: 1, turns: [canceledTurn] }))
        : pendingRefetch;
    }
    if (input === "/api/v1/workspace/conversations/conversation-1/messages/user-1/regenerate" && init?.method === "POST") {
      return Promise.resolve(response({
        conversation_revision: 3,
        attempt: attempt("attempt-after-cancel", "queued", 2)
      }, 202));
    }
    return baseFetch(input);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace("/workspace/conversation-1");
  fireEvent.click(await screen.findByRole("button", { name: "重试生成" }));

  await waitFor(() => expect(fetchMock.mock.calls.some((call) =>
    call[0] === "/api/v1/workspace/conversations/conversation-1/messages/user-1/regenerate"
  )).toBe(true));
  expect(await screen.findByRole("status")).toHaveTextContent("等待调度");
});

test("preserves variants and renders Markdown without unsafe links or raw HTML", async () => {
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000003");
  const firstTurn = {
    turn_no: 1,
    user_message: userMessage(1),
    assistant_messages: [assistantMessage("answer-first", 1, "第一轮回答")],
    attempts: [attempt("attempt-first", "succeeded", 1, "answer-first")]
  };
  const markdown = "## 新版本\n[外部](https://example.com) [危险](javascript:alert(1)) <img src=x onerror=alert(1)> ![远程追踪图](https://tracker.example/pixel.png)\n\n```js\nconst ok = true;\n```\n\n| A | B |\n| - | - |\n| 1 | 2 |";
  const latestTurn = {
    turn_no: 2,
    user_message: userMessage(2),
    assistant_messages: [
      assistantMessage("answer-old", 1, "旧版本", false),
      assistantMessage("answer-new", 2, markdown)
    ],
    attempts: [
      attempt("attempt-old", "succeeded", 1, "answer-old"),
      attempt("attempt-new", "succeeded", 2, "answer-new")
    ]
  };
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      return Promise.resolve(response({ ...conversation, last_turn_no: 2, turns: [firstTurn, latestTurn] }));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1/messages/user-2/regenerate" && init?.method === "POST") {
      return Promise.resolve(response({ conversation_revision: 3, attempt: attempt("attempt-3", "queued", 3) }));
    }
    return baseFetch(input);
  });
  vi.stubGlobal("fetch", fetchMock);

  const rendered = renderWorkspace("/workspace/conversation-1");

  const external = await screen.findByRole("link", { name: "外部" });
  expect(external).toHaveAttribute("target", "_blank");
  expect(external).toHaveAttribute("rel", "noopener noreferrer");
  expect(screen.getByText("危险").closest("a")).toBeNull();
  expect(rendered.container.querySelector("img")).toBeNull();
  expect(screen.getByText("[图片: 远程追踪图]")).toBeInTheDocument();
  expect(screen.getByText("const")).toBeInTheDocument();
  expect(rendered.container.querySelector(".workspace-table-scroll")).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "重新生成" })).toHaveLength(1);

  fireEvent.click(screen.getByRole("button", { name: "上一个回答" }));
  expect(screen.getByText("旧版本")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下一个回答" }));
  expect(screen.getByRole("heading", { name: "新版本" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
  await waitFor(() => expect(fetchMock.mock.calls.some((call) => call[0].endsWith("/regenerate"))).toBe(true));
});

test("does not clear or redirect a newly selected conversation when an old send completes", async () => {
  const secondConversation = {
    ...conversation,
    id: "conversation-2",
    title: "第二会话",
    revision: 5,
    updated_at: "2026-07-19T09:00:00Z"
  };
  let resolveSend: ((value: ReturnType<typeof response>) => void) | undefined;
  const sendPromise = new Promise<ReturnType<typeof response>>((resolve) => {
    resolveSend = resolve;
  });
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([agent]));
    if (input === "/api/v1/workspace/conversations") {
      return Promise.resolve(response({ items: [conversation, secondConversation] }));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1" && !init?.method) {
      return Promise.resolve(response({ ...conversation, turns: [] }));
    }
    if (input === "/api/v1/workspace/conversations/conversation-2" && !init?.method) {
      return Promise.resolve(response({ ...secondConversation, turns: [] }));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1/messages") return sendPromise;
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace("/workspace/conversation-1");
  const firstTextbox = await screen.findByRole("textbox", { name: "发送消息" });
  fireEvent.change(firstTextbox, { target: { value: "第一会话慢请求" } });
  fireEvent.keyDown(firstTextbox, { key: "Enter" });
  fireEvent.click(screen.getByRole("link", { name: /第二会话/ }));

  const secondTextbox = await screen.findByRole("textbox", { name: "发送消息" });
  await waitFor(() => expect(secondTextbox).toBeEnabled());
  fireEvent.change(secondTextbox, { target: { value: "第二会话草稿" } });
  resolveSend?.(response({
    conversation_revision: 3,
    user_message: userMessage(1, "第一会话慢请求"),
    attempt: attempt("attempt-late", "queued")
  }, 202));

  await waitFor(() => expect(secondTextbox).toHaveValue("第二会话草稿"));
  expect(screen.getAllByText("第二会话").length).toBeGreaterThan(0);
  expect(screen.queryByText("第一会话慢请求")).not.toBeInTheDocument();
});

test("offers migration when the pinned version is absent from the active catalog", async () => {
  const currentAgent = { ...agent, agent_version_id: "version-current", version: 5 };
  let detail = {
    ...conversation,
    agent_version_id: "version-retired",
    agent: { ...conversation.agent, agent_version_id: "version-retired", version: 2 },
    turns: []
  };
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([currentAgent]));
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [detail] }));
    if (input === "/api/v1/workspace/conversations/conversation-1" && !init?.method) {
      return Promise.resolve(response(detail));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1" && init?.method === "PATCH") {
      detail = {
        ...detail,
        revision: 3,
        agent_version_id: currentAgent.agent_version_id,
        agent: {
          ...detail.agent,
          agent_version_id: currentAgent.agent_version_id,
          version: currentAgent.version
        }
      };
      return Promise.resolve(response(detail));
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace("/workspace/conversation-1");
  fireEvent.click(await screen.findByRole("button", { name: "迁移到当前版本 V5" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/conversations/conversation-1",
    expect.objectContaining({
      method: "PATCH",
      body: JSON.stringify({ agent_version_id: "version-current", expected_revision: 2 })
    })
  ));
  expect(await screen.findByText("V5")).toBeInTheDocument();
});

test("renames and archives history with the latest expected revision", async () => {
  let current = { ...conversation, archived_at: null as string | null };
  let detail = { ...current, turns: [] as unknown[] };
  const patchBodies: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([agent]));
    if (input === "/api/v1/workspace/conversations" && !init?.method) {
      return Promise.resolve(response({ items: [current] }));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1" && !init?.method) {
      return Promise.resolve(response(detail));
    }
    if (input === "/api/v1/workspace/conversations/conversation-1" && init?.method === "PATCH") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      patchBodies.push(body);
      current = {
        ...current,
        ...(typeof body.title === "string" ? { title: body.title } : {}),
        ...(body.archived === true ? { archived_at: "2026-07-19T09:00:00Z" } : {}),
        revision: current.revision + 1
      };
      detail = { ...detail, ...current };
      return Promise.resolve(response(current));
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace("/workspace/conversation-1");
  fireEvent.click(await screen.findByRole("button", { name: "重命名 研发问答" }));
  expect(screen.getByLabelText("会话标题")).toHaveAttribute("maxlength", "200");
  fireEvent.change(screen.getByLabelText("会话标题"), { target: { value: "新的研究会话" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(await screen.findByRole("button", { name: "重命名 新的研究会话" })).toBeInTheDocument();

  await waitFor(() => expect(screen.getByRole("button", { name: "归档 新的研究会话" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "归档 新的研究会话" }));

  await waitFor(() => expect(patchBodies).toHaveLength(2));
  expect(patchBodies).toEqual([
    { title: "新的研究会话", expected_revision: 2 },
    { archived: true, expected_revision: 3 }
  ]);
  expect(await screen.findByRole("heading", { name: /建立一条可追溯的/ })).toBeInTheDocument();
});

test("saves the selected successful variant as an evaluation draft", async () => {
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000201");
  const successfulTurn = {
    turn_no: 1,
    user_message: userMessage(1),
    assistant_messages: [assistantMessage("answer-draft", 1, "可沉淀的回答")],
    attempts: [attempt("attempt-draft", "succeeded", 1, "answer-draft")]
  };
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      return Promise.resolve(response({ ...conversation, last_turn_no: 1, turns: [successfulTurn] }));
    }
    if (input === "/api/v1/evaluation-cases" && init?.method === "POST") {
      return Promise.resolve(response({ id: "case-1" }, 201));
    }
    return baseFetch(input);
  });
  vi.stubGlobal("fetch", fetchMock);

  const { queryClient } = renderWorkspace("/workspace/conversation-1");
  fireEvent.click(await screen.findByRole("button", { name: "保存为评测草稿" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/evaluation-cases",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        source_attempt_id: "attempt-draft",
        idempotency_key: "00000000-0000-4000-8000-000000000201"
      })
    })
  ));
  expect(queryClient.getQueryData(["workspace", "user-1", "evaluation-cases"])).toEqual({ items: [{ id: "case-1" }] });
  expect(await screen.findByText("案例编辑器占位")).toBeInTheDocument();
});

test("links an existing evaluation case for the selected successful attempt", async () => {
  const successfulTurn = {
    turn_no: 1,
    user_message: userMessage(1),
    assistant_messages: [assistantMessage("answer-existing", 1, "已有评测草稿的回答")],
    attempts: [attempt("attempt-existing", "succeeded", 1, "answer-existing")]
  };
  const fetchMock = vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      return Promise.resolve(response({ ...conversation, last_turn_no: 1, turns: [successfulTurn] }));
    }
    if (input === "/api/v1/evaluation-cases") {
      return Promise.resolve(response({
        items: [{ id: "case-existing", source: { attempt_id: "attempt-existing" } }]
      }));
    }
    return baseFetch(input);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderWorkspace("/workspace/conversation-1");

  expect(await screen.findByRole("link", { name: "查看评测草稿" })).toHaveAttribute(
    "href",
    "/evaluation-cases/case-existing"
  );
  expect(screen.queryByRole("button", { name: "保存为评测草稿" })).not.toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/evaluation-cases",
    expect.objectContaining({ credentials: "same-origin" })
  );
});

test("shows private conversation identity and hides all evaluation-draft actions", async () => {
  const privateWorkspaceAgent = { ...agent, visibility: "private" as const, model: "qwen-max" };
  const privateConversation = {
    ...conversation,
    agent: { ...conversation.agent, visibility: "private" as const, model: "qwen-max" }
  };
  const successfulTurn = {
    turn_no: 1,
    user_message: userMessage(1),
    assistant_messages: [assistantMessage("answer-private", 1, "私人模型回答")],
    attempts: [attempt("attempt-private", "succeeded", 1, "answer-private")]
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([privateWorkspaceAgent]));
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [privateConversation] }));
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      return Promise.resolve(response({ ...privateConversation, last_turn_no: 1, turns: [successfulTurn] }));
    }
    if (input === "/api/v1/evaluation-cases") {
      return Promise.resolve(response({ items: [{ id: "case-private", source: { attempt_id: "attempt-private" } }] }));
    }
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderWorkspace("/workspace/conversation-1", true);

  expect(await screen.findByText("私人模型回答")).toBeInTheDocument();
  expect(screen.getAllByText("私人 API").length).toBeGreaterThan(0);
  expect(screen.getAllByText("qwen-max").length).toBeGreaterThan(0);
  expect(screen.queryByRole("link", { name: "查看评测草稿" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "保存为评测草稿" })).not.toBeInTheDocument();
});

test("renders an explicit conversation history error", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([agent]));
    if (input === "/api/v1/evaluation-cases") return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/workspace/conversations") {
      return Promise.resolve(response({ detail: { message: "会话服务不可用" } }, 503));
    }
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderWorkspace();

  expect(await screen.findByRole("alert")).toHaveTextContent("会话历史读取失败：会话服务不可用");
  expect(screen.queryByText("暂无进行中的会话")).not.toBeInTheDocument();
});

test("keeps comparison available without showing draft controls to read-only users", async () => {
  const successfulTurn = {
    turn_no: 1,
    user_message: userMessage(1),
    assistant_messages: [assistantMessage("answer-read-only", 1, "只读回答")],
    attempts: [attempt("attempt-read-only", "succeeded", 1, "answer-read-only")]
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      return Promise.resolve(response({ ...conversation, last_turn_no: 1, turns: [successfulTurn] }));
    }
    return baseFetch(input);
  }));

  renderWorkspace("/workspace/conversation-1", false);

  expect(await screen.findByRole("link", { name: "并行对比" })).toHaveAttribute("href", "/compare");
  expect(screen.queryByRole("button", { name: "保存为评测草稿" })).not.toBeInTheDocument();
});

test("keeps the mobile drawer open until its explicit close control is used", async () => {
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
    matches: true,
    media: "(max-width: 900px)",
    addEventListener: vi.fn(),
    removeEventListener: vi.fn()
  }));
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/agent-options") return Promise.resolve(response([agent]));
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [] }));
    throw new Error(`Unexpected request: ${input}`);
  }));

  const rendered = renderWorkspace();
  const toggle = await screen.findByRole("button", { name: "打开会话列表" });
  const rail = rendered.container.querySelector("#workspace-conversation-rail");
  expect(rail).toHaveAttribute("aria-hidden", "true");
  expect(rail).toHaveAttribute("inert");
  expect(toggle).toHaveAttribute("aria-controls", "workspace-conversation-rail");
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(await screen.findByRole("dialog", { name: "会话列表" })).toHaveClass("is-open");
  expect(screen.getByRole("button", { name: "+ 新对话" })).toHaveFocus();
  expect(rail).not.toHaveAttribute("aria-hidden");
  expect(rail).not.toHaveAttribute("inert");
  const close = screen.getByRole("button", { name: "关闭会话列表" });
  const backdrop = rendered.container.querySelector(".conversation-drawer-backdrop");
  expect(backdrop).not.toHaveAttribute("hidden");

  fireEvent.click(backdrop!);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(rail).not.toHaveAttribute("aria-hidden");

  fireEvent.click(close);
  await waitFor(() => expect(toggle).toHaveFocus());
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(rail).toHaveAttribute("aria-hidden", "true");
});

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onerror: (() => void) | null = null;
  closed = false;
  private listeners = new Map<string, (event: { data: string }) => void>();
  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (event: { data: string }) => void) {
    this.listeners.set(type, listener);
  }
  close() {
    this.closed = true;
  }
  emit(type: string, data: unknown) {
    this.listeners.get(type)?.({ data: JSON.stringify(data) });
  }
}

function streamingDetailFetch(finalAnswer: string) {
  let detailCalls = 0;
  return vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/conversations/conversation-1") {
      detailCalls += 1;
      const runningTurn = {
        turn_no: 1,
        user_message: userMessage(1),
        assistant_messages: [],
        attempts: [attempt("attempt-1", "running")]
      };
      const succeededTurn = {
        ...runningTurn,
        assistant_messages: [assistantMessage("answer-1", 1, finalAnswer)],
        attempts: [attempt("attempt-1", "succeeded", 1, "answer-1")]
      };
      return Promise.resolve(response({
        ...conversation,
        last_turn_no: 1,
        turns: [detailCalls === 1 ? runningTurn : succeededTurn]
      }));
    }
    return baseFetch(input);
  });
}

test("renders streaming partial answers and refreshes on the done event", async () => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", streamingDetailFetch("流式最终回答"));

  renderWorkspace("/workspace/conversation-1");
  expect(await screen.findByRole("status")).toHaveTextContent("正在生成");
  await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
  const source = FakeEventSource.instances[0];
  expect(source.url).toBe("/api/v1/workspace/attempts/attempt-1/stream");

  act(() => source.emit("partial", { text: "流式片段" }));
  expect(await screen.findByText("流式片段")).toBeInTheDocument();
  expect(screen.queryByText("流式最终回答")).not.toBeInTheDocument();

  act(() => source.emit("done", { status: "succeeded", answer: "流式最终回答", error: null }));
  expect(source.closed).toBe(true);
  expect(await screen.findByText("流式最终回答")).toBeInTheDocument();
  expect(screen.queryByText("流式片段")).not.toBeInTheDocument();
});

test("falls back to polling when the attempt stream errors", async () => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", streamingDetailFetch("轮询兜底回答"));

  renderWorkspace("/workspace/conversation-1");
  expect(await screen.findByRole("status")).toHaveTextContent("正在生成");
  await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
  const source = FakeEventSource.instances[0];

  act(() => source.onerror?.());
  expect(source.closed).toBe(true);
  expect(await screen.findByText("轮询兜底回答", {}, { timeout: 2500 })).toBeInTheDocument();
});
