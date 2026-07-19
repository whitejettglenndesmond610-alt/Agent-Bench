import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { WorkspaceComparePage } from "./WorkspaceComparePage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? "Request failed" : "OK",
    json: async () => body
  };
}

const agents = [
  {
    agent_id: "agent-1",
    agent_version_id: "version-1",
    name: "Alpha Agent",
    provider: "Alpha",
    description: "Alpha research",
    logo_url: null,
    version: 2,
    max_prompt_characters: 4000,
    supports_citations: true
  },
  {
    agent_id: "agent-2",
    agent_version_id: "version-2",
    name: "Beta Agent",
    provider: "Beta",
    description: "Beta research",
    logo_url: null,
    version: 4,
    max_prompt_characters: null,
    supports_citations: false
  }
];

function attempt(id: string, status: "queued" | "running" | "succeeded" | "failed" | "canceled", answer?: string) {
  return {
    id,
    conversation_id: `conversation-${id}`,
    user_message_id: `message-${id}`,
    generation_no: 1,
    status,
    assistant_message_id: answer ? `answer-${id}` : null,
    agent_version_id: id === "attempt-1" ? "version-1" : "version-2",
    error_category: status === "failed" ? "provider" : null,
    assistant_message: answer ? {
      id: `answer-${id}`,
      turn_no: 1,
      role: "assistant",
      variant_no: 1,
      active: true,
      content: answer,
      citations: [],
      created_at: "2026-07-19T08:00:01Z"
    } : null,
    http_attempt_count: status === "queued" ? 0 : 1,
    last_http_status: status === "succeeded" ? 200 : status === "failed" ? 502 : null,
    latency_ms: status === "succeeded" ? 420 : null,
    input_tokens: status === "succeeded" ? 10 : null,
    output_tokens: status === "succeeded" ? 20 : null,
    total_tokens: status === "succeeded" ? 30 : null,
    error: status === "failed" ? { type: "upstream_error", category: "provider" } : null,
    cancel_requested_at: null,
    started_at: status === "queued" ? null : "2026-07-19T08:00:00Z",
    finished_at: status === "queued" ? null : "2026-07-19T08:00:01Z",
    created_at: "2026-07-19T08:00:00Z",
    updated_at: "2026-07-19T08:00:01Z"
  };
}

function comparison(status: "queued" | "completed") {
  const completed = status === "completed";
  return {
    id: "comparison-1",
    title: "解释可重复评测",
    prompt: "解释可重复评测",
    status,
    counts: completed
      ? { queued: 0, running: 0, succeeded: 1, failed: 1, canceled: 0 }
      : { queued: 2, running: 0, succeeded: 0, failed: 0, canceled: 0 },
    cancel_requested_at: null,
    created_at: "2026-07-19T08:00:00Z",
    updated_at: "2026-07-19T08:00:01Z",
    items: [
      {
        position: 1,
        conversation_id: "conversation-1",
        agent: { agent_id: "agent-1", agent_version_id: "version-1", name: "Alpha Agent", provider: "Alpha", version: 2 },
        attempt: completed ? attempt("attempt-1", "succeeded", "Alpha 的完整回答") : attempt("attempt-1", "queued"),
        evaluation_case_id: null as string | null
      },
      {
        position: 2,
        conversation_id: "conversation-2",
        agent: { agent_id: "agent-2", agent_version_id: "version-2", name: "Beta Agent", provider: "Beta", version: 4 },
        attempt: completed ? attempt("attempt-2", "failed") : attempt("attempt-2", "queued"),
        evaluation_case_id: null as string | null
      }
    ]
  };
}

function renderPage(initialEntry = "/compare", canDraft = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/compare" element={<WorkspaceComparePage userId="user-1" canDraft={canDraft} />} />
          <Route path="/compare/:comparisonId" element={<WorkspaceComparePage userId="user-1" canDraft={canDraft} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return Object.assign(rendered, { queryClient });
}

test("creates one persisted comparison batch for two unique Agents", async () => {
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000101");
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response(agents));
    if (input === "/api/v1/workspace/comparisons" && !init?.method) return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/workspace/comparisons" && init?.method === "POST") return Promise.resolve(response(comparison("queued"), 202));
    if (input === "/api/v1/workspace/comparisons/comparison-1") return Promise.resolve(response(comparison("queued")));
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderPage();
  const picker = await screen.findByRole("button", { name: "选择对比 Agent" });
  await waitFor(() => expect(picker).toBeEnabled());
  fireEvent.click(picker);
  fireEvent.click(await screen.findByRole("checkbox", { name: /Alpha Agent/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Beta Agent/ }));
  fireEvent.change(screen.getByRole("textbox", { name: "统一提示词" }), { target: { value: "解释可重复评测" } });
  fireEvent.click(screen.getByRole("button", { name: "启动 2 路并行对比" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/comparisons",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        prompt: "解释可重复评测",
        agent_version_ids: ["version-1", "version-2"],
        idempotency_key: "00000000-0000-4000-8000-000000000101"
      })
    })
  ));
  expect(await screen.findByText("PROMPT / IMMUTABLE")).toBeInTheDocument();
  expect(screen.getByText("解释可重复评测", { selector: ".comparison-prompt-readonly p" })).toBeInTheDocument();
});

test("preselects a query Agent only when its version is available", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response(agents));
    if (input === "/api/v1/workspace/comparisons") return Promise.resolve(response({ items: [] }));
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderPage("/compare?agentVersion=version-2");

  expect(await screen.findByLabelText("已选择 Agent")).toHaveTextContent("Beta Agent");
  expect(screen.getByLabelText("已选择 Agent")).not.toHaveTextContent("Alpha Agent");
  expect(screen.getByRole("button", { name: "选择对比 Agent" })).toHaveTextContent("已选择 1 个 Agent");
});

test("polls mixed results without hiding success and saves the successful answer as a draft", async () => {
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000102");
  let detailCalls = 0;
  let caseCreated = false;
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/comparisons") return Promise.resolve(response({ items: [comparison("queued")] }));
    if (input === "/api/v1/workspace/comparisons/comparison-1") {
      detailCalls += 1;
      const current = comparison(detailCalls === 1 ? "queued" : "completed");
      if (caseCreated) current.items[0].evaluation_case_id = "case-1";
      return Promise.resolve(response(current));
    }
    if (input === "/api/v1/evaluation-cases" && init?.method === "POST") {
      caseCreated = true;
      return Promise.resolve(response({ id: "case-1" }, 201));
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  const { queryClient } = renderPage("/compare/comparison-1");

  expect(await screen.findByText("Alpha 的完整回答", {}, { timeout: 2500 })).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("provider / upstream_error");
  expect(screen.getByText("420")).toBeInTheDocument();
  expect(screen.getAllByText("--").length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("button", { name: "保存为评测草稿" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/evaluation-cases",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        source_attempt_id: "attempt-1",
        idempotency_key: "00000000-0000-4000-8000-000000000102"
      })
    })
  ));
  expect(await screen.findByRole("link", { name: "查看评测草稿" })).toHaveAttribute("href", "/evaluation-cases/case-1");
  expect(queryClient.getQueryData(["workspace", "user-1", "evaluation-cases"])).toEqual({ items: [{ id: "case-1" }] });
  expect(detailCalls).toBeGreaterThanOrEqual(2);
});

test("polls history while a background comparison is active", async () => {
  let historyCalls = 0;
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response(agents));
    if (input === "/api/v1/workspace/comparisons") {
      historyCalls += 1;
      return Promise.resolve(response({ items: [comparison(historyCalls === 1 ? "queued" : "completed")] }));
    }
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderPage();

  expect(await screen.findByText("2 AGENTS · 等待调度")).toBeInTheDocument();
  expect(await screen.findByText("2 AGENTS · 已完成", {}, { timeout: 2500 })).toBeInTheDocument();
  expect(historyCalls).toBeGreaterThanOrEqual(2);
});

test("hides existing evaluation-case links from users without drafting permission", async () => {
  const completed = comparison("completed");
  completed.items[0].evaluation_case_id = "case-existing";
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/comparisons") return Promise.resolve(response({ items: [completed] }));
    if (input === "/api/v1/workspace/comparisons/comparison-1") return Promise.resolve(response(completed));
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderPage("/compare/comparison-1", false);

  expect(await screen.findByText("Alpha 的完整回答")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "查看评测草稿" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "保存为评测草稿" })).not.toBeInTheDocument();
});

test("applies a late draft response to its origin after switching comparisons", async () => {
  const first = comparison("completed");
  first.title = "第一对比";
  const second = { ...comparison("completed"), id: "comparison-2", title: "第二对比" };
  let resolveDraft: ((value: ReturnType<typeof response>) => void) | undefined;
  const draftResponse = new Promise<ReturnType<typeof response>>((resolve) => {
    resolveDraft = resolve;
  });
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/comparisons") return Promise.resolve(response({ items: [first, second] }));
    if (input === "/api/v1/workspace/comparisons/comparison-1") return Promise.resolve(response(first));
    if (input === "/api/v1/workspace/comparisons/comparison-2") return Promise.resolve(response(second));
    if (input === "/api/v1/evaluation-cases" && init?.method === "POST") return draftResponse;
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  const { queryClient } = renderPage("/compare/comparison-1");
  fireEvent.click(await screen.findByRole("button", { name: "保存为评测草稿" }));
  fireEvent.click(screen.getByRole("link", { name: /第二对比/ }));
  expect(await screen.findByRole("heading", { name: "第二对比" })).toBeInTheDocument();

  resolveDraft?.(response({ id: "case-late" }, 201));

  await waitFor(() => expect(queryClient.getQueryData(["workspace", "user-1", "evaluation-cases"])).toEqual({
    items: [{ id: "case-late" }]
  }));
  const origin = queryClient.getQueryData<ReturnType<typeof comparison>>([
    "workspace", "user-1", "comparison", "comparison-1"
  ]);
  expect(origin?.items[0].evaluation_case_id).toBe("case-late");
  expect(screen.queryByRole("link", { name: "查看评测草稿" })).not.toBeInTheDocument();
});

test("applies a late cancel response to its origin after switching comparisons", async () => {
  const first = comparison("queued");
  first.title = "第一运行中对比";
  const second = { ...comparison("completed"), id: "comparison-2", title: "第二已完成对比" };
  let resolveCancel: ((value: ReturnType<typeof response>) => void) | undefined;
  const cancelResponse = new Promise<ReturnType<typeof response>>((resolve) => {
    resolveCancel = resolve;
  });
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/comparisons") return Promise.resolve(response({ items: [first, second] }));
    if (input === "/api/v1/workspace/comparisons/comparison-1") return Promise.resolve(response(first));
    if (input === "/api/v1/workspace/comparisons/comparison-2") return Promise.resolve(response(second));
    if (input === "/api/v1/workspace/attempts/attempt-1/cancel" && init?.method === "POST") return cancelResponse;
    throw new Error(`Unexpected request: ${input}`);
  }));

  const { queryClient } = renderPage("/compare/comparison-1");
  fireEvent.click((await screen.findAllByRole("button", { name: "停止" }))[0]);
  fireEvent.click(screen.getByRole("link", { name: /第二已完成对比/ }));
  expect(await screen.findByRole("heading", { name: "第二已完成对比" })).toBeInTheDocument();

  resolveCancel?.(response(attempt("attempt-1", "canceled")));

  await waitFor(() => {
    const origin = queryClient.getQueryData<ReturnType<typeof comparison>>([
      "workspace", "user-1", "comparison", "comparison-1"
    ]);
    expect(origin?.items[0].attempt.status).toBe("canceled");
  });
  expect(screen.getByText("Alpha 的完整回答")).toBeInTheDocument();
});

test("renders an explicit comparison history error", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/agents") return Promise.resolve(response(agents));
    if (input === "/api/v1/workspace/comparisons") {
      return Promise.resolve(response({ detail: { message: "对比服务不可用" } }, 503));
    }
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderPage();

  expect(await screen.findByRole("alert")).toHaveTextContent("对比历史读取失败：对比服务不可用");
  expect(screen.queryByText("暂无对比记录")).not.toBeInTheDocument();
});
