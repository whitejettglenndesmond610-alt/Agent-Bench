import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { RecordsPage } from "./RecordsPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function response(body: object) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

const conversation = {
  id: "conversation-1",
  agent_version_id: "version-a",
  title: "研究会话",
  revision: 2,
  last_turn_no: 1,
  archived_at: null,
  created_at: "2026-07-18T08:00:00Z",
  updated_at: "2026-07-19T08:00:00Z",
  agent: { agent_id: "agent-a", agent_version_id: "version-a", name: "Agent Alpha", provider: "Provider A", version: 3, visibility: "public", model: null }
};

const attempt = {
  id: "attempt-1",
  status: "succeeded",
  assistant_message: { content: "对比回答", citations: [] }
};

const comparison = {
  id: "comparison-1",
  title: "方案对比",
  prompt: "比较两个方案",
  status: "completed",
  counts: { queued: 0, running: 0, succeeded: 1, failed: 0, canceled: 0 },
  revision: 4,
  archived_at: null,
  cancel_requested_at: null,
  created_at: "2026-07-18T08:00:00Z",
  updated_at: "2026-07-20T08:00:00Z",
  items: [{
    position: 0,
    conversation_id: "conversation-2",
    agent: { agent_id: "agent-a", agent_version_id: "version-a", name: "Agent Alpha", provider: "Provider A", version: 3, visibility: "public", model: null },
    attempt,
    evaluation_case_id: null
  }]
};

function renderPage(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><RecordsPage userId="user-1" /></MemoryRouter></QueryClientProvider>);
}

test("renames a record with revision protection and reruns a persisted comparison", async () => {
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/conversations" && !init?.method) return Promise.resolve(response({ items: [conversation] }));
    if (input === "/api/v1/workspace/comparisons" && !init?.method) return Promise.resolve(response({ items: [comparison] }));
    if (input === "/api/v1/workspace/conversations/conversation-1" && init?.method === "PATCH") {
      return Promise.resolve(response({ ...conversation, title: "更新后的研究会话", revision: 3 }));
    }
    if (input === "/api/v1/workspace/comparisons/comparison-1" && !init?.method) return Promise.resolve(response(comparison));
    if (input === "/api/v1/workspace/comparisons" && init?.method === "POST") {
      return Promise.resolve(response({ ...comparison, id: "comparison-2", title: "方案对比 · 再次运行", status: "queued" }));
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("crypto", { randomUUID: () => "idempotency-1" });
  renderPage(fetchMock);

  expect(await screen.findByRole("heading", { name: "我的记录" })).toBeInTheDocument();
  const conversationCard = (await screen.findByRole("link", { name: "研究会话" })).closest("article");
  fireEvent.click(within(conversationCard!).getByRole("button", { name: "重命名" }));
  fireEvent.change(screen.getByLabelText("记录标题"), { target: { value: "更新后的研究会话" } });
  fireEvent.click(screen.getByRole("button", { name: "保存标题" }));

  expect(await screen.findByRole("link", { name: "更新后的研究会话" })).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/conversations/conversation-1",
    expect.objectContaining({
      method: "PATCH",
      body: JSON.stringify({ expected_revision: 2, title: "更新后的研究会话" })
    })
  );

  const comparisonCard = screen.getByRole("link", { name: "方案对比" }).closest("article");
  fireEvent.click(within(comparisonCard!).getByRole("button", { name: "再次运行" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/comparisons",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        prompt: "比较两个方案",
        agent_version_ids: ["version-a"],
        title: "方案对比 · 再次运行",
        idempotency_key: "idempotency-1"
      })
    })
  ));
});

test("exports a complete conversation instead of the list summary", async () => {
  const detail = {
    ...conversation,
    turns: [{
      turn_no: 1,
      user_message: { content: "问题" },
      assistant_messages: [{ active: true, content: "完整回答", citations: ["https://example.com/source"] }],
      attempts: []
    }]
  };
  const fetchMock = vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/conversations") return Promise.resolve(response({ items: [conversation] }));
    if (input === "/api/v1/workspace/comparisons") return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/workspace/conversations/conversation-1") return Promise.resolve(response(detail));
    throw new Error(`Unexpected request: ${input}`);
  });
  const NativeURL = URL;
  class DownloadURL extends NativeURL {}
  DownloadURL.createObjectURL = vi.fn(() => "blob:record-export");
  DownloadURL.revokeObjectURL = vi.fn();
  vi.stubGlobal("URL", DownloadURL);
  const clickedDownloads: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    clickedDownloads.push(this.download);
  });
  renderPage(fetchMock);

  const title = await screen.findByRole("link", { name: "研究会话" });
  fireEvent.click(within(title.closest("article")!).getByRole("button", { name: "导出 Markdown" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/conversations/conversation-1",
    expect.any(Object)
  ));
  expect(DownloadURL.createObjectURL).toHaveBeenCalledTimes(1);
  expect(clickedDownloads).toContain("研究会话.md");
});
