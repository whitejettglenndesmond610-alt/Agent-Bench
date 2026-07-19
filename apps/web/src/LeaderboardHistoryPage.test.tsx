import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { LeaderboardHistoryPage, LeaderboardSnapshotPage } from "./LeaderboardHistoryPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function response(body: object) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body };
}

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

test("lists immutable leaderboard publications and marks the current snapshot", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([{
    id: "snapshot-current-123456",
    run_id: "run-1",
    run_name: "七月正式榜单",
    published_at: "2026-07-19T08:00:00Z",
    agent_count: 2,
    is_current: true
  }, {
    id: "snapshot-old-123456",
    run_id: "run-0",
    run_name: "六月正式榜单",
    published_at: "2026-06-19T08:00:00Z",
    agent_count: 1,
    is_current: false
  }])));
  render(<QueryClientProvider client={client()}><MemoryRouter><LeaderboardHistoryPage /></MemoryRouter></QueryClientProvider>);

  expect(await screen.findByRole("heading", { name: "榜单历史" })).toBeInTheDocument();
  expect(await screen.findByText("当前快照")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "七月正式榜单" })).toHaveAttribute("href", "/leaderboard/history/snapshot-current-123456");
  expect(screen.getByRole("link", { name: "六月正式榜单" })).toHaveAttribute("href", "/leaderboard/history/snapshot-old-123456");
});

test("renders and exports a published snapshot with public Agent links", async () => {
  const snapshot = {
    id: "snapshot-1",
    run_id: "run-1",
    run_name: "七月正式榜单",
    dataset_bundle_hash: "dataset-bundle-hash",
    published_at: "2026-07-19T08:00:00Z",
    rankings: [{
      rank: 1,
      run_agent_id: "run-agent-1",
      agent_id: "agent-a",
      agent_version_id: "version-a",
      agent_name: "Agent Alpha",
      provider: "Provider A",
      version: 3,
      overall_score: 92.5,
      ability_scores: {},
      language_scores: { zh: 92.5, en: 90 },
      benchmark_scores: [],
      answer_quality: 94,
      citation_quality: 88,
      success_rate: 98,
      anomaly_rate: 2,
      http_call_count: 100,
      latency_p50_ms: 120,
      latency_p95_ms: 350
    }]
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(snapshot)));
  const NativeURL = URL;
  class DownloadURL extends NativeURL {}
  DownloadURL.createObjectURL = vi.fn(() => "blob:snapshot-export");
  DownloadURL.revokeObjectURL = vi.fn();
  vi.stubGlobal("URL", DownloadURL);
  const clickedDownloads: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    clickedDownloads.push(this.download);
  });
  render(
    <QueryClientProvider client={client()}>
      <MemoryRouter initialEntries={["/leaderboard/history/snapshot-1"]}>
        <Routes><Route path="/leaderboard/history/:snapshotId" element={<LeaderboardSnapshotPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );

  expect(await screen.findByRole("heading", { name: "七月正式榜单" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Agent Alpha" })).toHaveAttribute("href", "/plaza/agent-a");
  expect(screen.getByText("dataset-bundle-hash")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "导出 Markdown" }));
  fireEvent.click(screen.getByRole("button", { name: "导出 JSON" }));
  expect(DownloadURL.createObjectURL).toHaveBeenCalledTimes(2);
  expect(clickedDownloads).toEqual(["七月正式榜单.md", "七月正式榜单.json"]);
});
