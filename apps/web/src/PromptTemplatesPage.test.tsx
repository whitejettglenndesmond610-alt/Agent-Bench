import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { PromptTemplatesPage } from "./PromptTemplatesPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function response(body: object) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

test("creates, edits, and deletes a revision-protected Prompt template", async () => {
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/prompt-templates" && !init?.method) return Promise.resolve(response({ items: [] }));
    if (input === "/api/v1/workspace/prompt-templates" && init?.method === "POST") return Promise.resolve(response({
      id: "template-1",
      name: "会议总结",
      content: "总结会议内容",
      revision: 1,
      created_at: "2026-07-20T08:00:00Z",
      updated_at: "2026-07-20T08:00:00Z"
    }));
    if (input === "/api/v1/workspace/prompt-templates/template-1" && init?.method === "PATCH") return Promise.resolve(response({
      id: "template-1",
      name: "会议纪要",
      content: "按行动项总结会议内容",
      revision: 2,
      created_at: "2026-07-20T08:00:00Z",
      updated_at: "2026-07-20T09:00:00Z"
    }));
    if (input === "/api/v1/workspace/prompt-templates/template-1" && init?.method === "DELETE") return Promise.resolve(response({}));
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><PromptTemplatesPage userId="user-1" /></MemoryRouter></QueryClientProvider>);

  expect(await screen.findByRole("heading", { name: "建立你的第一个 Prompt 模板" })).toBeInTheDocument();
  fireEvent.click(screen.getAllByRole("button", { name: /新建模板/ })[0]);
  fireEvent.change(screen.getByLabelText(/^模板名称/), { target: { value: "会议总结" } });
  fireEvent.change(screen.getByLabelText(/^Prompt 内容/), { target: { value: "总结会议内容" } });
  fireEvent.click(screen.getByRole("button", { name: "保存模板" }));

  expect(await screen.findByRole("heading", { name: "会议总结" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "用于对话" })).toHaveAttribute("href", "/workspace?template=template-1");
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/prompt-templates",
    expect.objectContaining({ method: "POST", body: JSON.stringify({ name: "会议总结", content: "总结会议内容" }) })
  );

  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByLabelText(/^模板名称/), { target: { value: "会议纪要" } });
  fireEvent.change(screen.getByLabelText(/^Prompt 内容/), { target: { value: "按行动项总结会议内容" } });
  fireEvent.click(screen.getByRole("button", { name: "保存模板" }));
  expect(await screen.findByRole("heading", { name: "会议纪要" })).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/prompt-templates/template-1",
    expect.objectContaining({ method: "PATCH", body: JSON.stringify({ expected_revision: 1, name: "会议纪要", content: "按行动项总结会议内容" }) })
  );

  fireEvent.click(screen.getByRole("button", { name: "删除" }));
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(screen.queryByRole("heading", { name: "会议纪要" })).not.toBeInTheDocument());
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/workspace/prompt-templates/template-1",
    expect.objectContaining({ method: "DELETE", body: JSON.stringify({ expected_revision: 2 }) })
  );
});
