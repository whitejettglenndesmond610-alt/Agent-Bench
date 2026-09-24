import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { LeaderboardSnapshot } from "./api";
import { DeltaMark } from "./DeltaMark";
import { deltaForMetric } from "./leaderboardDelta";

afterEach(cleanup);

function snapshot(rankings: Array<Record<string, unknown>>): LeaderboardSnapshot {
  return {
    id: "snapshot-1",
    run_id: "run-1",
    run_name: "历史快照",
    dataset_bundle_hash: "hash",
    published_at: "2026-06-20T00:00:00Z",
    rankings: rankings.map((ranking) => ({
      rank: 1,
      run_agent_id: "run-agent",
      agent_id: "agent",
      agent_version_id: "version",
      agent_name: "Agent",
      provider: "Provider",
      version: 1,
      overall_score: 80,
      ability_scores: {},
      language_scores: { zh: 80, en: 80 },
      benchmark_scores: [],
      answer_quality: 80,
      citation_quality: 80,
      success_rate: 99,
      anomaly_rate: 0,
      http_call_count: 10,
      ...ranking
    }))
  } as LeaderboardSnapshot;
}

test("deltaForMetric computes rank and score movement for the overall metric", () => {
  const previous = snapshot([{ agent_id: "a", rank: 3, overall_score: 88.5 }]);
  expect(deltaForMetric(previous, "a", "overall", { rank: 1, score: 93.25 }))
    .toEqual({ rankDelta: 2, scoreDelta: 93.25 - 88.5 });
});

test("deltaForMetric uses ability scores and drops rank delta for specialist metrics", () => {
  const previous = snapshot([{ agent_id: "a", rank: 1, overall_score: 88, ability_scores: { retrieval: 70 } }]);
  expect(deltaForMetric(previous, "a", "retrieval", { rank: 2, score: 76 }))
    .toEqual({ rankDelta: null, scoreDelta: 6 });
});

test("deltaForMetric returns null when the agent or score is missing from the snapshot", () => {
  const previous = snapshot([{ agent_id: "a", rank: 1, overall_score: 88 }]);
  expect(deltaForMetric(previous, "missing", "overall", { rank: 1, score: 90 })).toBeNull();
  expect(deltaForMetric(previous, "a", "overall", { rank: 1, score: null })).toBeNull();
  expect(deltaForMetric(null, "a", "overall", { rank: 1, score: 90 })).toBeNull();
});

test("DeltaMark renders direction and signed score, and nothing for null delta", () => {
  const { unmount } = render(<DeltaMark delta={{ rankDelta: 2, scoreDelta: 2.2 }} />);
  const mark = screen.getByLabelText(/较上一发布快照/);
  expect(mark).toHaveTextContent("↑2");
  expect(mark).toHaveTextContent("+2.20");
  expect(mark.className).toContain("is-up");
  unmount();

  render(<DeltaMark delta={null} />);
  expect(screen.queryByLabelText(/较上一发布快照/)).not.toBeInTheDocument();
});
