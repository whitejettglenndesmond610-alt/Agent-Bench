import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { EvaluationCase } from "./api";
import { EvaluationCasesPage } from "./EvaluationCasesPage";

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

const baseCase: EvaluationCase = {
  id: "case-1",
  status: "draft",
  revision: 0,
  prompt: "原始问题",
  reference_answer: null,
  language: null,
  ability: null,
  question_type: "short_answer",
  requires_citations: false,
  difficulty: null,
  domain: null,
  review_notes: null,
  standalone_verified: false,
  reference_answer_verified: false,
  rights_verified: false,
  sensitive_data_verified: false,
  source_hash: "source-hash",
  record_hash: null,
  reviewed_at: null,
  published_at: null,
  created_at: "2026-07-19T08:00:00Z",
  updated_at: "2026-07-19T08:00:00Z",
  source: {
    attempt_id: "attempt-1",
    conversation_id: "conversation-1",
    comparison_id: "comparison-1",
    user_message: "原始问题",
    candidate_answer: "## 候选回答\n来自 Agent 的不可变回答。",
    citations: ["https://example.com/source"],
    generation_no: 1,
    prompt_format: "workspace-v1",
    prompt_hash: "prompt-hash",
    latency_ms: 560,
    input_tokens: 12,
    output_tokens: 24,
    total_tokens: 36,
    artifact_sha256: "artifact-hash",
    agent: {
      agent_id: "agent-1",
      agent_version_id: "version-1",
      name: "Atlas Agent",
      provider: "Atlas",
      version: 3
    }
  }
};

function renderPage(canPublish = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/evaluation-cases/case-1"]}>
        <Routes>
          <Route path="/evaluation-cases/:caseId" element={<EvaluationCasesPage userId="operator-1" canPublish={canPublish} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

test("saves, explicitly reviews, warns about curated-only publication, and publishes immutably", async () => {
  let current = baseCase;
  const bodies: Array<{ path: string; body: Record<string, unknown> }> = [];
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-cases" && !init?.method) return Promise.resolve(response({ items: [current] }));
    if (input === "/api/v1/evaluation-cases/case-1" && !init?.method) return Promise.resolve(response(current));
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    bodies.push({ path: input, body });
    if (input === "/api/v1/evaluation-cases/case-1" && init?.method === "PATCH") {
      current = {
        ...current,
        ...body,
        status: "draft",
        revision: 1,
        reference_answer: body.reference_answer as string,
        language: body.language as "zh",
        ability: body.ability as "reasoning",
        updated_at: "2026-07-19T08:01:00Z"
      };
      return Promise.resolve(response(current));
    }
    if (input === "/api/v1/evaluation-cases/case-1/review" && init?.method === "POST") {
      current = {
        ...current,
        status: "reviewed",
        revision: 2,
        standalone_verified: true,
        reference_answer_verified: true,
        rights_verified: true,
        sensitive_data_verified: true,
        record_hash: "record-hash",
        reviewed_at: "2026-07-19T08:02:00Z"
      };
      return Promise.resolve(response(current));
    }
    if (input === "/api/v1/evaluation-cases/case-1/publish" && init?.method === "POST") {
      current = {
        ...current,
        status: "published",
        revision: 3,
        published_at: "2026-07-19T08:03:00Z"
      };
      return Promise.resolve(response(current));
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderPage();
  expect(await screen.findByRole("heading", { name: "候选回答" })).toBeInTheDocument();
  expect(screen.getByLabelText("参考答案")).toHaveValue("");
  fireEvent.change(screen.getByLabelText("参考答案"), { target: { value: "人工编写的参考答案" } });
  fireEvent.change(screen.getByLabelText("语言"), { target: { value: "zh" } });
  fireEvent.change(screen.getByLabelText("能力维度"), { target: { value: "reasoning" } });
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

  await waitFor(() => expect(bodies.some((item) => item.path === "/api/v1/evaluation-cases/case-1" && item.body.expected_revision === 0)).toBe(true));
  await waitFor(() => expect(screen.getByText(/更新于/)).toHaveTextContent("REV 1"));
  const reviewButton = screen.getByRole("button", { name: "确认复核" });
  expect(reviewButton).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "案例可脱离原对话独立理解" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "参考答案已经人工核验" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "内容来源与使用权已确认" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "不包含敏感或个人数据" }));
  fireEvent.click(reviewButton);

  await waitFor(() => expect(bodies).toContainEqual({
    path: "/api/v1/evaluation-cases/case-1/review",
    body: {
      expected_revision: 1,
      standalone_verified: true,
      reference_answer_verified: true,
      rights_verified: true,
      sensitive_data_verified: true
    }
  }));
  expect(await screen.findByText(/发布仅进入精选案例库/)).toBeInTheDocument();
  expect(screen.getByText(/不会进入正式数据集、评测运行或排行榜/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox", { name: /我确认本次发布仅用于精选案例库/ }));
  fireEvent.click(screen.getByRole("button", { name: "发布到精选案例库" }));

  await waitFor(() => expect(bodies).toContainEqual({
    path: "/api/v1/evaluation-cases/case-1/publish",
    body: { expected_revision: 2, confirm_curated_only: true }
  }));
  expect(await screen.findByText("已发布案例不可修改。内容和记录哈希已锁定。")).toBeInTheDocument();
  expect(screen.getByLabelText("评测问题")).toBeDisabled();
});

test("does not review a draft with unsaved editable fields", async () => {
  const completeDraft: EvaluationCase = {
    ...baseCase,
    reference_answer: "已保存的参考答案",
    language: "zh",
    ability: "reasoning"
  };
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-cases" && !init?.method) return Promise.resolve(response({ items: [completeDraft] }));
    if (input === "/api/v1/evaluation-cases/case-1" && !init?.method) return Promise.resolve(response(completeDraft));
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderPage();
  await screen.findByRole("heading", { name: "评测案例编辑器" });
  fireEvent.click(screen.getByRole("checkbox", { name: "案例可脱离原对话独立理解" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "参考答案已经人工核验" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "内容来源与使用权已确认" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "不包含敏感或个人数据" }));
  const reviewButton = screen.getByRole("button", { name: "确认复核" });
  expect(reviewButton).toBeEnabled();

  fireEvent.change(screen.getByLabelText("领域"), { target: { value: "未保存领域" } });

  expect(reviewButton).toBeDisabled();
  const hint = screen.getByRole("status");
  expect(hint).toHaveTextContent("有未保存修改，请先保存修改。");
  expect(reviewButton).toHaveAttribute("aria-describedby", hint.id);
  fireEvent.click(reviewButton);
  expect(fetchMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(false);
});

test("does not publish a reviewed case with unsaved editable fields", async () => {
  const reviewedCase: EvaluationCase = {
    ...baseCase,
    status: "reviewed",
    revision: 4,
    reference_answer: "已复核的参考答案",
    language: "zh",
    ability: "reasoning",
    standalone_verified: true,
    reference_answer_verified: true,
    rights_verified: true,
    sensitive_data_verified: true,
    record_hash: "record-hash",
    reviewed_at: "2026-07-19T08:02:00Z"
  };
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-cases" && !init?.method) return Promise.resolve(response({ items: [reviewedCase] }));
    if (input === "/api/v1/evaluation-cases/case-1" && !init?.method) return Promise.resolve(response(reviewedCase));
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  renderPage();
  const saveButton = await screen.findByRole("button", { name: "保存修改" });
  expect(saveButton).toBeDisabled();
  fireEvent.click(saveButton);
  expect(fetchMock.mock.calls.some((call) => call[1]?.method === "PATCH")).toBe(false);
  fireEvent.click(await screen.findByRole("checkbox", { name: /我确认本次发布仅用于精选案例库/ }));
  const publishButton = screen.getByRole("button", { name: "发布到精选案例库" });
  expect(publishButton).toBeEnabled();

  fireEvent.change(screen.getByLabelText("难度"), { target: { value: "未保存难度" } });

  expect(publishButton).toBeDisabled();
  const hint = screen.getByRole("status");
  expect(hint).toHaveTextContent("有未保存修改，请先保存修改。");
  expect(publishButton).toHaveAttribute("aria-describedby", hint.id);
  fireEvent.click(publishButton);
  expect(fetchMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(false);
});

test("refetches and explains revision conflicts in Chinese", async () => {
  let detailCalls = 0;
  const newer = { ...baseCase, revision: 2, prompt: "其他位置更新后的问题" };
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-cases") return Promise.resolve(response({ items: [detailCalls ? newer : baseCase] }));
    if (input === "/api/v1/evaluation-cases/case-1" && !init?.method) {
      detailCalls += 1;
      return Promise.resolve(response(detailCalls === 1 ? baseCase : newer));
    }
    if (input === "/api/v1/evaluation-cases/case-1" && init?.method === "PATCH") {
      return Promise.resolve(response({ detail: { code: "revision_conflict", message: "Revision changed" } }, 409));
    }
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderPage();
  fireEvent.change(await screen.findByLabelText("评测问题"), { target: { value: "触发冲突的本地修改" } });
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("已重新读取最新版本");
  expect(await screen.findByLabelText("评测问题")).toHaveValue("其他位置更新后的问题");
  expect(detailCalls).toBeGreaterThanOrEqual(2);
});

test("renders an explicit evaluation-case history error", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/evaluation-cases") {
      return Promise.resolve(response({ detail: { message: "案例服务不可用" } }, 503));
    }
    if (input === "/api/v1/evaluation-cases/case-1") return Promise.resolve(response(baseCase));
    throw new Error(`Unexpected request: ${input}`);
  }));

  renderPage();

  expect(await screen.findByRole("alert")).toHaveTextContent("案例历史读取失败：案例服务不可用");
  expect(screen.queryByText("暂无评测草稿")).not.toBeInTheDocument();
});
