import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { RunEvidencePage } from "./RunEvidencePage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function response(body: unknown) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

const baseItem = {
  execution_item_id: "item-1", run_agent_id: "run-agent-1", agent_id: "agent-1", agent_version_id: "version-1",
  agent_name: "Atlas Agent", provider: "Atlas", version: 2, question_id: "question-1", question_external_id: "Q-001",
  question_type: "open_ended", benchmark_id: "benchmark-1", benchmark_name: "Research Bench", ability: "retrieval",
  language: "en", requires_citations: true, difficulty: "hard", domain: "research", status: "scored",
  error_type: null, answer_preview: "<img src=x onerror=alert(1)>", attempt_count: 1, latency_ms: 430,
  score: { score_id: "score-1", answer_score: 85, citation_score: 75, final_score: 82 },
  created_at: "2026-07-19T08:00:00Z", updated_at: "2026-07-19T08:01:00Z"
};

function renderPage(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[entry]}><Routes>
    <Route path="/runs/:runId/evidence" element={<RunEvidencePage />} />
    <Route path="/runs/:runId/evidence/:executionItemId" element={<RunEvidencePage />} />
  </Routes></MemoryRouter></QueryClientProvider>);
}

test("lists scored, failed, and pending evidence without inventing missing scores", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input.startsWith("/api/v1/evaluation-runs/run-1/evidence?")) return Promise.resolve(response({
      run_id: "run-1", total: 3, limit: 25, offset: 0,
      items: [
        baseItem,
        { ...baseItem, execution_item_id: "item-2", question_external_id: "Q-002", status: "failed", error_type: "agent_timeout", score: null, answer_preview: null },
        { ...baseItem, execution_item_id: "item-3", question_external_id: "Q-003", status: "pending", score: null, answer_preview: null }
      ]
    }));
    throw new Error(`Unexpected request: ${input}`);
  }));
  const rendered = renderPage("/runs/run-1/evidence");

  expect(await screen.findByRole("heading", { name: "运行证据" })).toBeInTheDocument();
  expect(screen.getByText("已评分")).toBeInTheDocument();
  expect(screen.getByText("执行失败")).toBeInTheDocument();
  expect(screen.getByText("等待执行")).toBeInTheDocument();
  expect(await screen.findByText("82.00")).toBeInTheDocument();
  expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  expect(screen.getByText("agent_timeout")).toBeInTheDocument();
  expect(rendered.container.querySelector("img")).toBeNull();
});

test("renders detail attempts, judge reason, citations, and retention metadata as safe content", async () => {
  const sha = "a".repeat(64);
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/evaluation-runs/run-1/evidence/item-1") return Promise.resolve(response({
      ...baseItem,
      prompt: "Explain <script>alert(1)</script>", choices: null, reference_answer: "Trusted reference",
      candidate_answer: "Agent says <b>not HTML</b>", candidate_answer_status: "available",
      attempts: [{ attempt_id: "attempt-1", attempt_no: 1, status: "succeeded", started_at: "2026-07-19T08:00:00Z", finished_at: "2026-07-19T08:00:01Z", status_code: 200, latency_ms: 430, input_tokens: 10, output_tokens: 20, total_tokens: 30, error_type: null }],
      score: { ...baseItem.score, judge_reason: "Strong answer with direct support.", judge_artifact: null },
      citations: [{ citation_id: "citation-1", position: 0, original_url: "javascript:alert(1)", final_url: null, fetch_status: "blocked", http_status: null, authenticity_score: null, support_score: null, judge_reason: "Unsafe scheme blocked", content_artifact: null, judge_artifact: null }],
      response_artifact: { artifact_id: "artifact-1", kind: "agent_response", sha256: sha, size_bytes: 512, expires_at: "2026-08-19T08:00:00Z", retained: false }
    }));
    throw new Error(`Unexpected request: ${input}`);
  }));
  const rendered = renderPage("/runs/run-1/evidence/item-1");

  expect(await screen.findByRole("heading", { name: "Q-001" })).toBeInTheDocument();
  expect(screen.getByText("Strong answer with direct support.")).toBeInTheDocument();
  expect(screen.getByText("尝试 1 · succeeded")).toBeInTheDocument();
  expect(screen.getByText("Unsafe scheme blocked")).toBeInTheDocument();
  expect(screen.getByText(sha)).toBeInTheDocument();
  expect(rendered.container.querySelector("script")).toBeNull();
  expect(rendered.container.querySelector("b b")).toBeNull();
  expect(screen.queryByRole("link", { name: "javascript:alert(1)" })).not.toBeInTheDocument();
  expect(screen.queryByText("0.00")).not.toBeInTheDocument();
});
