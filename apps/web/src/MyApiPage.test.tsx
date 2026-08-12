import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { PrivateAgent } from "./api";
import { MyApiPage } from "./MyApiPage";
import { userQueryKeys } from "./userProduct";

afterEach(() => {
  cleanup();
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

function privateAgent(overrides: Partial<PrivateAgent> = {}): PrivateAgent {
  return {
    agent_id: "private-1",
    agent_version_id: "private-version-1",
    active_agent_version_id: null,
    active_provider: null,
    active_model: null,
    name: "我的 Qwen",
    description: "私人研究模型",
    preset: "dashscope",
    provider: "阿里百炼",
    model: "qwen-plus",
    version: 1,
    health_status: "untested",
    health_checked_at: null,
    health_latency_ms: null,
    health_error_code: null,
    workspace_available: false,
    created_at: "2026-07-20T08:00:00Z",
    updated_at: "2026-07-20T08:00:00Z",
    ...overrides
  };
}

function renderPage(isShared = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } }
  });
  queryClient.setQueryData(userQueryKeys.agentOptions("user-1"), [{ agent_id: "public-1" }]);
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <MyApiPage userId="user-1" isShared={isShared} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return Object.assign(rendered, { queryClient });
}

test("creates, tests, edits, and revokes a private API without echoing its key", async () => {
  let agents: PrivateAgent[] = [];
  const mutationBodies: Array<{ method: string; body: Record<string, unknown> }> = [];
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/private-agents" && !init?.method) {
      return Promise.resolve(response({ items: agents }));
    }
    if (input === "/api/v1/workspace/private-agents" && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      mutationBodies.push({ method: "POST", body });
      agents = [privateAgent({
        name: String(body.name),
        description: String(body.description),
        preset: body.preset as PrivateAgent["preset"],
        model: String(body.model)
      })];
      return Promise.resolve(response(agents[0], 201));
    }
    if (input === "/api/v1/workspace/private-agents/private-1/test" && init?.method === "POST") {
      agents = [{ ...agents[0],
        active_agent_version_id: "private-version-1",
        active_provider: "阿里百炼",
        active_model: "qwen-max",
        health_status: "healthy",
        health_checked_at: "2026-07-20T09:00:00Z",
        health_latency_ms: 128,
        workspace_available: true
      }];
      return Promise.resolve(response({ ...agents[0], success: true, status_code: 200, latency_ms: 128, error_code: null }));
    }
    if (input === "/api/v1/workspace/private-agents/private-1" && init?.method === "PATCH") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      mutationBodies.push({ method: "PATCH", body });
      agents = [privateAgent({
        name: String(body.name),
        description: String(body.description),
        model: String(body.model),
        agent_version_id: "private-version-2",
        active_agent_version_id: "private-version-1",
        active_provider: "阿里百炼",
        active_model: "qwen-max",
        version: 2,
        health_status: "untested",
        health_checked_at: null,
        health_latency_ms: null,
        workspace_available: true
      })];
      return Promise.resolve(response(agents[0]));
    }
    if (input === "/api/v1/workspace/private-agents/private-1" && init?.method === "DELETE") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      mutationBodies.push({ method: "DELETE", body });
      agents = [];
      return Promise.resolve(response({}, 204));
    }
    throw new Error(`Unexpected request: ${input} ${init?.method ?? "GET"}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  const { queryClient } = renderPage();
  expect(await screen.findByRole("heading", { name: "还没有私人 API" })).toBeInTheDocument();
  expect(screen.getByText("共享会话风险")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "+ 添加 API" }));
  fireEvent.change(screen.getByLabelText(/^名称/), { target: { value: "团队 Qwen" } });
  fireEvent.change(screen.getByLabelText("描述（可选）"), { target: { value: "内部研究" } });
  fireEvent.change(screen.getByLabelText(/^模型/), { target: { value: "qwen-max" } });
  fireEvent.change(document.querySelector("#private-agent-key")!, { target: { value: "sk-never-echo-this" } });
  fireEvent.click(screen.getByRole("button", { name: "添加 API" }));

  expect(await screen.findByRole("heading", { name: "团队 Qwen" })).toBeInTheDocument();
  expect(screen.queryByText("sk-never-echo-this")).not.toBeInTheDocument();
  expect(screen.queryByDisplayValue("sk-never-echo-this")).not.toBeInTheDocument();
  expect(mutationBodies[0]).toEqual({
    method: "POST",
    body: {
      name: "团队 Qwen",
      description: "内部研究",
      preset: "dashscope",
      model: "qwen-max",
      api_key: "sk-never-echo-this"
    }
  });

  fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
  expect(await screen.findByText("健康")).toBeInTheDocument();
  expect(screen.getByText("128 ms")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "用于在线对话" })).toHaveAttribute("href", "/workspace?agentVersion=private-version-1");
  expect(screen.getByRole("link", { name: "用于多 Agent 对比" })).toHaveAttribute("href", "/compare?agentVersion=private-version-1");

  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  expect(screen.getByLabelText("替换 API Key（可选）")).toHaveValue("");
  fireEvent.change(screen.getByLabelText(/^名称/), { target: { value: "团队 Qwen 2" } });
  fireEvent.change(screen.getByLabelText(/^模型/), { target: { value: "qwen-max-latest" } });
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));

  expect(await screen.findByRole("heading", { name: "团队 Qwen 2" })).toBeInTheDocument();
  expect(mutationBodies[1]).toEqual({
    method: "PATCH",
    body: {
      expected_version: 1,
      name: "团队 Qwen 2",
      description: "内部研究",
      preset: "dashscope",
      model: "qwen-max-latest"
    }
  });

  fireEvent.click(screen.getByRole("button", { name: "撤销并删除" }));
  fireEvent.click(screen.getByRole("button", { name: "确认撤销并删除" }));
  expect(await screen.findByRole("heading", { name: "还没有私人 API" })).toBeInTheDocument();
  expect(mutationBodies[2]).toEqual({ method: "DELETE", body: { expected_version: 2 } });
  await waitFor(() => expect(queryClient.getQueryState(userQueryKeys.agentOptions("user-1"))?.isInvalidated).toBe(true));
});

test("explains controlled presets, encrypted storage, provider prompt transfer, and named-account retention", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ items: [] })));
  renderPage(false);

  expect(await screen.findByText(/配置保存在当前账号下/)).toBeInTheDocument();
  expect(screen.getByText(/请求地址由平台控制/)).toBeInTheDocument();
  expect(screen.getByText(/API Key 加密保存且永不回显/)).toBeInTheDocument();
  expect(screen.getByText(/提示词和对话上下文会发送给所选模型提供方/)).toBeInTheDocument();
});

test("normalizes display names and renders rejected tests as actionable errors", async () => {
  let savedBody: Record<string, unknown> | null = null;
  let agents: PrivateAgent[] = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/private-agents" && !init?.method) {
      return Promise.resolve(response({ items: agents }));
    }
    if (input === "/api/v1/workspace/private-agents" && init?.method === "POST") {
      savedBody = JSON.parse(String(init.body)) as Record<string, unknown>;
      agents = [privateAgent({
        preset: "deepseek",
        provider: "DeepSeek",
        model: String(savedBody.model),
        name: String(savedBody.name)
      })];
      return Promise.resolve(response(agents[0], 201));
    }
    if (input === "/api/v1/workspace/private-agents/private-1/test" && init?.method === "POST") {
      agents = [{ ...agents[0], health_status: "unhealthy", health_error_code: "request_rejected", health_checked_at: "2026-07-20T09:00:00Z", health_latency_ms: 108 }];
      return Promise.resolve(response({ ...agents[0], success: false, status_code: 400, latency_ms: 108, error_code: "request_rejected" }));
    }
    throw new Error(`Unexpected request: ${input} ${init?.method ?? "GET"}`);
  }));
  renderPage();

  await screen.findByRole("heading", { name: "还没有私人 API" });
  fireEvent.click(screen.getByRole("button", { name: "+ 添加 API" }));
  fireEvent.change(screen.getByLabelText(/^名称/), { target: { value: "DeepSeek 私人模型" } });
  fireEvent.change(screen.getByLabelText(/^提供方预设/), { target: { value: "deepseek" } });
  expect(screen.getByText(/必须填写 API model ID，例如 deepseek-v4-pro/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/^模型 ID/), { target: { value: "DeepSeek V4 Pro" } });
  fireEvent.change(document.querySelector("#private-agent-key")!, { target: { value: "sk-private" } });
  fireEvent.click(screen.getByRole("button", { name: "添加 API" }));

  await screen.findByRole("heading", { name: "DeepSeek 私人模型" });
  expect(savedBody).toMatchObject({ preset: "deepseek", model: "deepseek-v4-pro" });
  fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveClass("is-error");
  expect(alert).toHaveTextContent("API model ID");
  expect(alert).toHaveTextContent("HTTP 400");
});

test("remaps retired deepseek-chat model id before create", async () => {
  let savedBody: Record<string, unknown> | null = null;
  let agents: PrivateAgent[] = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/workspace/private-agents" && !init?.method) {
      return Promise.resolve(response({ items: agents }));
    }
    if (input === "/api/v1/workspace/private-agents" && init?.method === "POST") {
      savedBody = JSON.parse(String(init.body)) as Record<string, unknown>;
      agents = [privateAgent({
        name: String(savedBody.name),
        preset: "deepseek",
        provider: "DeepSeek",
        model: String(savedBody.model)
      })];
      return Promise.resolve(response(agents[0], 201));
    }
    throw new Error(`Unexpected request: ${input} ${init?.method ?? "GET"}`);
  }));
  renderPage(false);

  await screen.findByRole("heading", { name: "还没有私人 API" });
  fireEvent.click(screen.getByRole("button", { name: "+ 添加 API" }));
  fireEvent.change(screen.getByLabelText(/^名称/), { target: { value: "旧别名" } });
  fireEvent.change(screen.getByLabelText(/^提供方预设/), { target: { value: "deepseek" } });
  fireEvent.change(screen.getByLabelText(/^模型 ID/), { target: { value: "deepseek-chat" } });
  fireEvent.change(document.querySelector("#private-agent-key")!, { target: { value: "sk-private" } });
  fireEvent.click(screen.getByRole("button", { name: "添加 API" }));

  await screen.findByRole("heading", { name: "旧别名" });
  expect(savedBody).toMatchObject({ preset: "deepseek", model: "deepseek-v4-flash" });
});
