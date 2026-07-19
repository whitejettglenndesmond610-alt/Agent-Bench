import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { ExperiencePage } from "./ExperiencePage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function response(body: object) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

const tasks = [{
  slug: "source-check",
  title: "核验公开来源",
  summary: "比较多个公开来源并说明差异。",
  prompt: "请核验以下说法并引用来源。",
  category: "retrieval_citations",
  difficulty: "intermediate",
  recommended_mode: "comparison",
  min_agents: 2,
  max_agents: 3,
  ranking_eligible: false
}, {
  slug: "memo",
  title: "提炼会议行动项",
  summary: "整理会议记录。",
  prompt: "提炼行动项。",
  category: "structured_extraction",
  difficulty: "beginner",
  recommended_mode: "chat",
  min_agents: 1,
  max_agents: 1,
  ranking_eligible: false
}];

function renderPage(entry = "/experience") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes><Route path="/experience/:taskSlug?" element={<ExperiencePage userId="user-1" />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

test("filters non-ranking practice tasks and links to the recommended workspace", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(tasks)));
  renderPage();

  expect(await screen.findByRole("heading", { name: "体验任务" })).toBeInTheDocument();
  expect(await screen.findByRole("link", { name: "核验公开来源" })).toHaveAttribute("href", "/experience/source-check");
  expect(screen.getByText("体验结果不影响正式排行榜")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("筛选推荐模式"), { target: { value: "chat" } });
  expect(screen.queryByRole("link", { name: "核验公开来源" })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "提炼会议行动项" })).toBeInTheDocument();
});

test("shows task details without reference answers or Judge data", async () => {
  const fetchMock = vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/workspace/experience-tasks") return Promise.resolve(response(tasks));
    if (input === "/api/v1/workspace/experience-tasks/source-check") return Promise.resolve(response(tasks[0]));
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage("/experience/source-check");

  expect(await screen.findByRole("heading", { name: "核验公开来源" })).toBeInTheDocument();
  expect(screen.getByText("请核验以下说法并引用来源。")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "用单 Agent 开始" })).toHaveAttribute("href", "/workspace?task=source-check");
  expect(screen.getByRole("link", { name: "用多 Agent 对比" })).toHaveAttribute("href", "/compare?task=source-check");
  expect(screen.getByText(/ranking_eligible = false/)).toBeInTheDocument();
  expect(screen.queryByText(/参考答案：/)).not.toBeInTheDocument();
});
