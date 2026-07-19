import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { ExperimentsPage } from "./ExperimentsPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function response(body: unknown) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

const counts = { draft: 0, queued: 0, running: 0, paused: 0, scoring: 0, completed: 2, published: 0, system_failed: 0, canceled: 0 };
const result = { overall_score: 88, ability_scores: { reasoning: 91 }, answer_quality: 87, citation_quality: 80, success_rate: 98, anomaly_rate: 2, http_call_count: 42, validity_status: "valid", validity_reason: null, latency_p50_ms: 420, latency_p95_ms: 900, total_tokens: 1200 };

const detail = {
  id: "experiment-1", name: "双 Agent 正式实验", creator: { user_id: "user-1", subject_key: "operator-1", display_name: "评测员" },
  status: "completed", protocol_hash: "same-protocol-hash", profile_key: "unified-v3", run_scope: "comprehensive", target_ability: null,
  member_count: 2, run_counts: counts, total: 140, processed: 140, completed: 139, failed: 1, progress_percent: 100,
  created_at: "2026-07-19T08:00:00Z", updated_at: "2026-07-19T09:00:00Z",
  members: [
    { position: 1, run_id: "run-1", run_name: "Alpha run", status: "completed", stage: "ready_to_publish", agent_id: "agent-1", agent_version_id: "version-1", agent_name: "Alpha Agent", provider: "Alpha", version: 2, total: 70, processed: 70, completed: 70, failed: 0, retried: 1, progress_percent: 100, result, metric_scores: { overall: 88, reasoning: 91 }, metric_ranks: { overall: 1, reasoning: 1 } },
    { position: 2, run_id: "run-2", run_name: "Beta run", status: "completed", stage: "ready_to_publish", agent_id: "agent-2", agent_version_id: "version-2", agent_name: "Beta Agent", provider: "Beta", version: 4, total: 70, processed: 70, completed: 69, failed: 1, retried: 0, progress_percent: 100, result: null, metric_scores: {}, metric_ranks: {} }
  ],
  comparison_metrics: [{ metric: "overall", protocol_hash: "same-protocol-hash", compared_count: 1 }, { metric: "reasoning", protocol_hash: "same-protocol-hash", compared_count: 1 }]
};

function renderPage(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[entry]}><Routes>
    <Route path="/experiments" element={<ExperimentsPage />} />
    <Route path="/experiments/:experimentId" element={<ExperimentsPage />} />
  </Routes></MemoryRouter></QueryClientProvider>);
}

test("lists persisted experiment summaries", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([detail])));
  renderPage("/experiments");
  expect(await screen.findByRole("heading", { name: "评测实验" })).toBeInTheDocument();
  expect(await screen.findByRole("link", { name: "双 Agent 正式实验" })).toHaveAttribute("href", "/experiments/experiment-1");
  expect(screen.getByText("100.00%")).toBeInTheDocument();
  expect(screen.getByText("评测员")).toBeInTheDocument();
});

test("shows member operations and marks every missing result unavailable instead of zero", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(detail)));
  renderPage("/experiments/experiment-1");
  expect(await screen.findByRole("heading", { name: "双 Agent 正式实验" })).toBeInTheDocument();
  expect(screen.getAllByText("88.00")).toHaveLength(2);
  expect(screen.getAllByText("不可用").length).toBeGreaterThanOrEqual(4);
  expect(screen.queryByText("0.00")).not.toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: "子运行" })[0]).toHaveAttribute("href", "/runs/run-1");
  expect(screen.getAllByRole("link", { name: "运行证据" })[0]).toHaveAttribute("href", "/runs/run-1/evidence");
  expect(screen.getAllByRole("link", { name: "Agent 详情" })[1]).toHaveAttribute("href", "/agents/agent-2");
  expect(screen.getByText("—", { selector: ".experiment-member-score strong" })).toBeInTheDocument();
});
