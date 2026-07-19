import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { WorkspaceAttempt } from "./api";
import { AttemptAnnotationControls } from "./userProduct";

const attempt: WorkspaceAttempt = {
  id: "attempt-1",
  conversation_id: "conversation-1",
  user_message_id: "message-1",
  generation_no: 1,
  status: "succeeded",
  assistant_message_id: "assistant-1",
  agent_version_id: "version-1",
  error_category: null,
  assistant_message: {
    id: "assistant-1",
    turn_no: 1,
    role: "assistant",
    variant_no: 1,
    active: true,
    content: "Answer",
    citations: [],
    created_at: "2026-07-20T03:00:00Z"
  },
  http_attempt_count: 1,
  last_http_status: 200,
  latency_ms: 100,
  input_tokens: 5,
  output_tokens: 6,
  total_tokens: 11,
  feedback: null,
  bookmarked: false,
  private_note: null,
  error: null,
  cancel_requested_at: null,
  started_at: "2026-07-20T03:00:00Z",
  finished_at: "2026-07-20T03:00:01Z",
  created_at: "2026-07-20T03:00:00Z",
  updated_at: "2026-07-20T03:00:01Z"
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderControls() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AttemptAnnotationControls userId="user-1" attempt={attempt} />
    </QueryClientProvider>
  );
}

test("sends the backend up/down feedback contract", async () => {
  const fetchMock = vi.fn().mockImplementation((_input: string, init?: RequestInit) =>
    Promise.resolve({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ ...attempt, feedback: JSON.parse(String(init?.body)).feedback })
    })
  );
  vi.stubGlobal("fetch", fetchMock);
  document.cookie = "agent_eval_csrf=csrf-token; path=/";
  renderControls();

  fireEvent.click(screen.getByRole("button", { name: "回答有帮助" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/attempts/attempt-1/annotation",
    expect.objectContaining({
      method: "PATCH",
      body: JSON.stringify({ feedback: "up" })
    })
  ));
});

test("persists a private note without sending it to an Agent route", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => ({ ...attempt, private_note: "Verify source two" })
  });
  vi.stubGlobal("fetch", fetchMock);
  document.cookie = "agent_eval_csrf=csrf-token; path=/";
  renderControls();

  fireEvent.click(screen.getByText("添加私人笔记"));
  fireEvent.change(screen.getByLabelText("私人笔记"), {
    target: { value: "Verify source two" }
  });
  fireEvent.click(screen.getByRole("button", { name: "保存笔记" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/attempts/attempt-1/annotation",
    expect.objectContaining({
      method: "PATCH",
      body: JSON.stringify({ private_note: "Verify source two" })
    })
  ));
  expect(fetchMock).not.toHaveBeenCalledWith(
    expect.stringContaining("/messages"),
    expect.anything()
  );
});
