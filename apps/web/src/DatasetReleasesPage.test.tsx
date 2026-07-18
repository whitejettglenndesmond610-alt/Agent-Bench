import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { DatasetRelease, EvaluationCase } from "./api";
import { DatasetReleasesPage } from "./DatasetReleasesPage";

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

const evaluationCase: EvaluationCase = {
  id: "case-1",
  status: "published",
  revision: 5,
  prompt: "解释可重复评测为什么需要冻结数据集",
  reference_answer: "冻结内容可确保重复运行具有相同输入。",
  language: "zh",
  ability: "reasoning",
  question_type: "short_answer",
  requires_citations: false,
  difficulty: "medium",
  domain: "evaluation",
  review_notes: null,
  standalone_verified: true,
  reference_answer_verified: true,
  rights_verified: true,
  sensitive_data_verified: true,
  source_hash: "a".repeat(64),
  record_hash: "b".repeat(64),
  reviewed_at: "2026-07-19T08:00:00Z",
  published_at: "2026-07-19T08:01:00Z",
  created_at: "2026-07-19T07:00:00Z",
  updated_at: "2026-07-19T08:01:00Z",
  source: {
    attempt_id: "attempt-1",
    conversation_id: "conversation-1",
    comparison_id: null,
    user_message: "问题",
    candidate_answer: "回答",
    citations: [],
    generation_no: 1,
    prompt_format: "workspace-v1",
    prompt_hash: "c".repeat(64),
    latency_ms: 100,
    input_tokens: 5,
    output_tokens: 6,
    total_tokens: 11,
    artifact_sha256: "d".repeat(64),
    agent: {
      agent_id: "agent-1",
      agent_version_id: "version-1",
      name: "Alpha Agent",
      provider: "Alpha",
      version: 1
    }
  }
};

function makeRelease(status: DatasetRelease["status"], revision: number): DatasetRelease {
  return {
    id: "release-1",
    name: "2026 夏季精选发布",
    description: "人工复核案例",
    status,
    revision,
    manifest_hash: status === "draft" ? null : "e".repeat(64),
    approved_by_user_id: status === "draft" ? null : "operator-1",
    approved_at: status === "draft" ? null : "2026-07-19T09:00:00Z",
    published_by_user_id: status === "published" ? "operator-1" : null,
    published_at: status === "published" ? "2026-07-19T09:01:00Z" : null,
    created_at: "2026-07-19T08:30:00Z",
    updated_at: "2026-07-19T09:01:00Z",
    cases: [{
      id: evaluationCase.id,
      position: 1,
      prompt: evaluationCase.prompt,
      language: "zh",
      ability: "reasoning",
      question_type: "short_answer",
      record_hash: evaluationCase.record_hash!
    }],
    dataset_versions: status === "published" ? [{
      id: "dataset-1",
      benchmark_name: "Curated-release-reasoning",
      language: "zh",
      ability: "reasoning",
      version: 1,
      question_count: 1,
      content_hash: "f".repeat(64),
      run_eligible: false
    }] : []
  };
}

test("creates, approves, and publishes a run-isolated formal dataset release", async () => {
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000201");
  let current: DatasetRelease | null = null;
  const bodies: Array<{ path: string; body: Record<string, unknown> }> = [];
  const fetchMock = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-cases?status=published&limit=100") {
      return Promise.resolve(response({ items: [evaluationCase] }));
    }
    if (input === "/api/v1/dataset-releases" && !init?.method) {
      return Promise.resolve(response({ items: current ? [current] : [] }));
    }
    if (input === "/api/v1/dataset-releases" && init?.method === "POST") {
      bodies.push({ path: input, body: JSON.parse(String(init.body)) as Record<string, unknown> });
      current = makeRelease("draft", 0);
      return Promise.resolve(response(current, 201));
    }
    if (input === "/api/v1/dataset-releases/release-1" && !init?.method) {
      return Promise.resolve(response(current));
    }
    if (input === "/api/v1/dataset-releases/release-1/approve" && init?.method === "POST") {
      bodies.push({ path: input, body: JSON.parse(String(init.body)) as Record<string, unknown> });
      current = makeRelease("approved", 1);
      return Promise.resolve(response(current));
    }
    if (input === "/api/v1/dataset-releases/release-1/publish" && init?.method === "POST") {
      bodies.push({ path: input, body: JSON.parse(String(init.body)) as Record<string, unknown> });
      current = makeRelease("published", 2);
      return Promise.resolve(response(current));
    }
    throw new Error(`Unexpected request: ${input}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } }
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/dataset-releases"]}>
        <Routes>
          <Route path="/dataset-releases" element={<DatasetReleasesPage userId="operator-1" canPublish />} />
          <Route path="/dataset-releases/:releaseId" element={<DatasetReleasesPage userId="operator-1" canPublish />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );

  fireEvent.change(screen.getByRole("textbox", { name: "发布名称" }), {
    target: { value: "2026 夏季精选发布" }
  });
  fireEvent.click(await screen.findByRole("checkbox", { name: /选择案例 解释可重复评测/ }));
  fireEvent.click(screen.getByRole("button", { name: "建立发布草稿" }));

  expect(await screen.findByRole("heading", { name: "2026 夏季精选发布" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox", { name: /逐项核对案例内容/ }));
  fireEvent.click(screen.getByRole("button", { name: "批准发布清单" }));
  expect(await screen.findByText("这些版本保持 `run_eligible=false`，不进入 Profile、Run 或排行榜。")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("checkbox", { name: /不会影响排行榜/ }));
  fireEvent.click(screen.getByRole("button", { name: "正式发布不可运行版本" }));
  expect(await screen.findByText("RUN ISOLATED")).toBeInTheDocument();
  expect(screen.getByText("NO")).toBeInTheDocument();

  await waitFor(() => expect(bodies).toEqual([
    {
      path: "/api/v1/dataset-releases",
      body: {
        name: "2026 夏季精选发布",
        description: null,
        evaluation_case_ids: ["case-1"],
        idempotency_key: "00000000-0000-4000-8000-000000000201"
      }
    },
    {
      path: "/api/v1/dataset-releases/release-1/approve",
      body: { expected_revision: 0, confirm_case_selection: true }
    },
    {
      path: "/api/v1/dataset-releases/release-1/publish",
      body: { expected_revision: 1, confirm_not_run_eligible: true }
    }
  ]));
});
