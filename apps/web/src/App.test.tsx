import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
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

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } }
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <App />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

test("shows the login form without a session", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({}, 401)));

  renderApp();

  expect(await screen.findByRole("heading", { name: "选择访问身份" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /只读访问/ })).toHaveAttribute(
    "aria-pressed",
    "true"
  );
});

test("shows the live platform baseline for an operator", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: string) => {
      if (input === "/api/v1/auth/session") {
        return Promise.resolve(
          response({ role: "operator", expires_at: "2026-01-01T08:00:00Z" })
        );
      }
      return Promise.resolve(
        response({
          name: "Agent Evaluation Platform",
          version: "0.1.0",
          environment: "test",
          status: "ready"
        })
      );
    })
  );

  renderApp();

  expect(await screen.findByText("服务在线")).toBeInTheDocument();
  expect(screen.getByText("test")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Agent" })).toBeInTheDocument();
  expect(screen.queryByText("legacy-v1")).not.toBeInTheDocument();
  expect(screen.getByText("unified-v3")).toBeInTheDocument();
  expect(screen.getByText("UNIFIED V3 / ACTIVE")).toBeInTheDocument();
  expect(screen.getByText("13")).toBeInTheDocument();
  expect(screen.getByLabelText("Unified V3 协议状态")).toHaveTextContent("中文 100");
  expect(screen.getByLabelText("Unified V3 协议状态")).toHaveTextContent("英文 75");
  expect(screen.getByRole("link", { name: "发起综合评测" })).toHaveAttribute(
    "href",
    "/runs?launch=1&profile=unified-v3"
  );
  expect(screen.getByRole("link", { name: "查看排行榜" })).toHaveAttribute("href", "/leaderboard");
});

test("sends a read-only session directly to the global leaderboard", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: string) => {
      if (input === "/api/v1/auth/session") {
        return Promise.resolve(
          response({ role: "read_only", expires_at: "2026-01-01T08:00:00Z" })
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
  expect(screen.queryByRole("link", { name: "总览" })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "排行榜" })).toBeInTheDocument();
});
