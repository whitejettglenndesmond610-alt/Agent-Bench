import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { AgentsPage } from "./AgentsPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.cookie = "agent_eval_csrf=; Max-Age=0";
});

function response(body: object, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 422 ? "Unprocessable Entity" : "OK",
    json: async () => body
  };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } }
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AgentsPage />
    </QueryClientProvider>
  );
}

function beginConfiguration() {
  fireEvent.click(screen.getByRole("button", { name: "+ 接入 Agent" }));
  fireEvent.change(screen.getByLabelText(/^Agent 名称/), { target: { value: "Demo Agent" } });
  fireEvent.change(screen.getByLabelText(/^提供方/), { target: { value: "Demo" } });
  fireEvent.click(screen.getByRole("button", { name: "下一步：接口配置" }));
  fireEvent.change(screen.getByLabelText(/^接口地址/), {
    target: { value: "https://agent.example/run" }
  });
  fireEvent.change(screen.getByLabelText(/^鉴权方式/), { target: { value: "bearer" } });
  fireEvent.change(screen.getByLabelText(/^Bearer Token/), {
    target: { value: "plain-secret" }
  });
  fireEvent.change(screen.getByLabelText(/^最大 Prompt 字符数/), {
    target: { value: "4000" }
  });
  fireEvent.click(screen.getByRole("button", { name: "下一步：连接测试" }));
}

test("tests a connection before creating an agent version", async () => {
  let created = false;
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-runs/agent-statuses") {
      return Promise.resolve(response([]));
    }
    if (input === "/api/v1/agents" && !init?.method) {
      return Promise.resolve(
        response(
          created
            ? [
                {
                  id: "agent-id",
                  name: "Demo Agent",
                  provider: "Demo",
                  description: null,
                  logo_url: null,
                  status: "enabled",
                  created_at: "2026-07-16T00:00:00Z",
                  updated_at: "2026-07-16T00:00:00Z"
                }
              ]
            : []
        )
      );
    }
    if (input === "/api/v1/agents/test-connection") {
      const body = JSON.parse(String(init?.body));
      expect(body.token).toBe("plain-secret");
      expect(body.max_prompt_characters).toBe(4000);
      expect(init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-token" });
      return Promise.resolve(
        response({
          success: true,
          status_code: 200,
          latency_ms: 18,
          request_body: { query: body.prompt },
          raw_response: { answer: "Connected" },
          answer: "Connected",
          citations: ["https://example.com/source"],
          error: null
        })
      );
    }
    if (input === "/api/v1/agents" && init?.method === "POST") {
      created = true;
      return Promise.resolve(
        response({
          id: "agent-id",
          name: "Demo Agent",
          provider: "Demo",
          description: null,
          logo_url: null,
          status: "enabled",
          created_at: "2026-07-16T00:00:00Z",
          updated_at: "2026-07-16T00:00:00Z"
        }, 201)
      );
    }
    if (input === "/api/v1/agents/agent-id/versions") {
      return Promise.resolve(
        response({
          id: "version-id",
          agent_id: "agent-id",
          version: 1,
          endpoint_url: "https://agent.example/run",
          auth_type: "bearer",
          token_configured: true,
          request_template: '{"query":"{{prompt}}"}',
          answer_jsonpath: "$.answer",
          citations_jsonpath: "$.citations",
          timeout_seconds: 120,
          concurrency: 5,
          max_prompt_characters: 4000,
          lifecycle_status: "draft",
          preflight_status: "not_run",
          preflight_report: [],
          preflight_at: null,
          enabled: true,
          created_at: "2026-07-16T00:00:00Z"
        }, 201)
      );
    }
    if (input === "/api/v1/agent-versions/version-id/preflight") {
      return Promise.resolve(response({
        version_id: "version-id",
        lifecycle_status: "verified",
        preflight_status: "passed",
        preflight_at: "2026-07-16T00:05:00Z",
        checks: [{
          key: "basic",
          label: "基础连接与答案提取",
          status: "passed",
          prompt_characters: 8,
          status_code: 200,
          latency_ms: 20,
          error_category: null,
          error: null
        }]
      }));
    }
    if (input === "/api/v1/agent-versions/version-id/promote") {
      return Promise.resolve(response({
        id: "version-id",
        agent_id: "agent-id",
        version: 1,
        endpoint_url: "https://agent.example/run",
        auth_type: "bearer",
        token_configured: true,
        request_template: '{"query":"{{prompt}}"}',
        answer_jsonpath: "$.answer",
        citations_jsonpath: "$.citations",
        timeout_seconds: 120,
        concurrency: 5,
        max_prompt_characters: 4000,
        lifecycle_status: "active",
        preflight_status: "passed",
        preflight_report: [],
        preflight_at: "2026-07-16T00:05:00Z",
        enabled: true,
        created_at: "2026-07-16T00:00:00Z"
      }));
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  document.cookie = "agent_eval_csrf=csrf-token";
  renderPage();

  expect(await screen.findByRole("heading", { name: "尚未接入 Agent" })).toBeInTheDocument();
  beginConfiguration();
  fireEvent.click(screen.getByRole("button", { name: "发送测试请求" }));

  expect(await screen.findByText("连接与提取成功")).toBeInTheDocument();
  expect(screen.getAllByText("Connected").length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("button", { name: "创建 Agent 并保存" }));

  expect(await screen.findByText("等待自动预检")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "运行自动预检" }));
  expect(await screen.findByText("预检通过，可以晋升")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "运行 Smoke" })).toHaveAttribute(
    "href",
    "/runs?launch=1&agentVersion=version-id&profile=smoke"
  );
  fireEvent.click(screen.getByRole("button", { name: "设为当前版本" }));

  expect(await screen.findByRole("status")).toHaveTextContent("Demo Agent 的 V1 已设为当前版本");
  await waitFor(() => expect(screen.getByText("Demo Agent")).toBeInTheDocument());
});

test("returns validation errors to the matching configuration field", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: string) => {
      if (input === "/api/v1/evaluation-runs/agent-statuses") {
        return Promise.resolve(response([]));
      }
      if (input === "/api/v1/agents") return Promise.resolve(response([]));
      if (input === "/api/v1/agents/test-connection") {
        return Promise.resolve(
          response(
            {
              detail: [
                {
                  type: "value_error",
                  loc: ["body", "answer_jsonpath"],
                  msg: "Value error, invalid JSONPath expression"
                }
              ]
            },
            422
          )
        );
      }
      throw new Error(`Unexpected request: ${input}`);
    })
  );
  renderPage();

  await screen.findByRole("heading", { name: "尚未接入 Agent" });
  beginConfiguration();
  fireEvent.click(screen.getByRole("button", { name: "发送测试请求" }));

  expect(await screen.findByText("invalid JSONPath expression")).toBeInTheDocument();
  expect(screen.getByLabelText(/^答案 JSONPath/)).toHaveAttribute("aria-invalid", "true");
});

test("renders the Agent evaluation matrix with active-run and quick-evaluate links", async () => {
  const metric = (
    status: "not_evaluated" | "queued" | "running" | "evaluated" | "needs_retest" | "failed",
    score: number | null = null,
    runId: string | null = null
  ) => ({
    status,
    score,
    run_id: runId,
    source_version: score == null ? null : 3,
    updated_at: "2026-07-16T08:00:00Z"
  });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: string) => {
      if (input === "/api/v1/agents") {
        return Promise.resolve(response([
          {
            id: "agent-1",
            name: "Agent Alpha",
            provider: "Provider A",
            description: "主力 Agent",
            logo_url: null,
            status: "enabled",
            created_at: "2026-07-16T00:00:00Z",
            updated_at: "2026-07-16T08:00:00Z"
          },
          {
            id: "agent-2",
            name: "Agent Disabled",
            provider: "Provider B",
            description: null,
            logo_url: null,
            status: "disabled",
            created_at: "2026-07-16T00:00:00Z",
            updated_at: "2026-07-16T08:00:00Z"
          }
        ]));
      }
      if (input === "/api/v1/evaluation-runs/agent-statuses") {
        return Promise.resolve(response([
          {
            agent_id: "agent-1",
            agent_version_id: "version-3",
            agent_name: "Agent Alpha",
            provider: "Provider A",
            version: 3,
            max_prompt_characters: 4000,
            supports_citations: false,
            comprehensive: metric("evaluated", 86.5, "run-comprehensive"),
            reasoning: metric("needs_retest", 81, "run-reasoning"),
            retrieval: metric("running", null, "run-active"),
            data_reasoning: metric("failed", null, "run-failed"),
            instruction_following: metric("evaluated", 88, "run-instruction"),
            long_context: metric("not_evaluated")
          },
          {
            agent_id: "agent-2",
            agent_version_id: "version-1",
            agent_name: "Agent Disabled",
            provider: "Provider B",
            version: 1,
            max_prompt_characters: null,
            supports_citations: true,
            comprehensive: metric("not_evaluated"),
            reasoning: metric("not_evaluated"),
            retrieval: metric("not_evaluated"),
            data_reasoning: metric("not_evaluated"),
            instruction_following: metric("not_evaluated"),
            long_context: metric("not_evaluated")
          }
        ]));
      }
      throw new Error(`Unexpected request: ${input}`);
    })
  );
  renderPage();

  expect(await screen.findByText("Agent Alpha")).toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "综合" })).toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "推理" })).toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "检索" })).toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "数据推理" })).toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "指令遵循" })).toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "长上下文" })).toBeInTheDocument();
  expect(screen.getByText("86.5")).toBeInTheDocument();
  expect(screen.getByText("待复测")).toBeInTheDocument();
  expect(screen.getByText("失败")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Agent Alpha 检索评测中，查看任务" })).toHaveAttribute(
    "href",
    "/runs/run-active"
  );
  expect(screen.getByRole("link", { name: "评测 Agent Alpha 的推理能力" })).toHaveAttribute(
    "href",
    "/runs?launch=1&agentVersion=version-3&profile=unified-v3-reasoning"
  );
  expect(screen.getByRole("link", { name: "评测 Agent Alpha 的综合能力" })).toHaveAttribute(
    "href",
    "/runs?launch=1&agentVersion=version-3&profile=unified-v3"
  );
  expect(screen.queryByRole("link", { name: /评测 Agent Disabled/ })).not.toBeInTheDocument();
  expect(screen.getByText("停用")).toBeInTheDocument();
  expect(screen.getByText(/V3 · ≤ 4000 字符 · 无引用/)).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "配置新版本" })).toHaveLength(2);
});
