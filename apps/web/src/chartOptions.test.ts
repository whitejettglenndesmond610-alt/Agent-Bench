import { expect, test } from "vitest";

import { AgentScoreHistoryPoint } from "./api";
import { abilityRadarOption, chartPalette, scoreTrendOption } from "./chartOptions";
import { abilityMetrics } from "./metrics";

interface RadarOptionShape {
  color: string[];
  legend: { data: string[] };
  radar: { indicator: Array<{ name: string; min: number; max: number }> };
  series: Array<{ type: string; data: Array<{ name: string; value: Array<number | null> }> }>;
}

interface TrendOptionShape {
  color: string[];
  xAxis: { type: string; data: string[] };
  yAxis: { min: number; max: number };
  series: Array<{ type: string; name: string; data: number[] }>;
}

function historyPoint(score: number, publishedAt: string): AgentScoreHistoryPoint {
  return {
    score,
    protocol_hash: "protocol-hash",
    protocol_matches_current: true,
    is_current_source: false,
    agent_version_id: "version-1",
    version: 1,
    source_snapshot_id: `snapshot-${publishedAt}`,
    source_run_id: "run-1",
    source_run_name: "正式评测",
    source_profile_key: "unified-v3",
    source_published_at: publishedAt,
    benchmark_count: 1
  };
}

test("builds a fixed-range radar option covering all ability dimensions", () => {
  const option = abilityRadarOption([
    { name: "Agent Alpha · V1", values: [90, 80, 70, 60, 50] },
    { name: "Agent Beta · V2", values: [80, 70, 60, 50, null] }
  ]) as unknown as RadarOptionShape;

  expect(option.color).toEqual(chartPalette);
  expect(option.radar.indicator).toHaveLength(abilityMetrics.length);
  expect(option.radar.indicator.every((indicator) => indicator.min === 0 && indicator.max === 100)).toBe(true);
  expect(option.legend.data).toEqual(["Agent Alpha · V1", "Agent Beta · V2"]);
  expect(option.series[0].type).toBe("radar");
  expect(option.series[0].data[0]).toEqual({ name: "Agent Alpha · V1", value: [90, 80, 70, 60, 50] });
  // Missing ability scores stay null instead of being coerced to 0.
  expect(option.series[0].data[1].value).toEqual([80, 70, 60, 50, null]);
});

test("builds a trend option ordered by publication time", () => {
  const { ordered, option } = scoreTrendOption([
    historyPoint(92.5, "2026-07-19T08:00:00Z"),
    historyPoint(88, "2026-06-19T08:00:00Z"),
    historyPoint(90.25, "2026-07-01T08:00:00Z")
  ]);
  const shape = option as unknown as TrendOptionShape;

  expect(ordered.map((point) => point.score)).toEqual([88, 90.25, 92.5]);
  expect(shape.xAxis.data).toEqual(["2026-06-19", "2026-07-01", "2026-07-19"]);
  expect(shape.yAxis.min).toBe(0);
  expect(shape.yAxis.max).toBe(100);
  expect(shape.series[0].type).toBe("line");
  expect(shape.series[0].data).toEqual([88, 90.25, 92.5]);
  expect(shape.color).toEqual([chartPalette[0]]);
});

test("builds an empty trend option for agents without history", () => {
  const { ordered, option } = scoreTrendOption([]);
  const shape = option as unknown as TrendOptionShape;

  expect(ordered).toEqual([]);
  expect(shape.xAxis.data).toEqual([]);
  expect(shape.series[0].data).toEqual([]);
});
