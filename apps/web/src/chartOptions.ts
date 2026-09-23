import type { EChartsCoreOption } from "echarts/core";

import { AgentScoreHistoryPoint } from "./api";
import { abilityMetrics } from "./metrics";

// Mirrors design-tokens.css: --accent, --cyan, --violet, --rose, --green.
export const chartPalette = ["#4f46e5", "#0891b2", "#7c3aed", "#e11d48", "#059669"];

export interface RadarEntry {
  name: string;
  values: Array<number | null>;
}

export function abilityRadarOption(entries: RadarEntry[]): EChartsCoreOption {
  return {
    color: chartPalette,
    aria: { enabled: true, decal: { show: true } },
    tooltip: { trigger: "item" },
    legend: {
      bottom: 0,
      textStyle: { color: "#64748b", fontSize: 10 },
      data: entries.map((entry) => entry.name)
    },
    radar: {
      center: ["50%", "43%"],
      radius: "62%",
      splitNumber: 5,
      indicator: abilityMetrics.map((ability) => ({ name: ability.label, max: 100, min: 0 })),
      axisName: { color: "#64748b", fontSize: 10 },
      splitArea: { areaStyle: { color: ["#ffffff", "#f4f6ff"] } },
      splitLine: { lineStyle: { color: "#dce3ee" } },
      axisLine: { lineStyle: { color: "#cbd5e1" } }
    },
    series: [{
      type: "radar",
      symbolSize: 5,
      lineStyle: { width: 2 },
      areaStyle: { opacity: 0.12 },
      data: entries.map((entry) => ({ name: entry.name, value: entry.values }))
    }]
  };
}

export interface ScoreTrendSeries {
  ordered: AgentScoreHistoryPoint[];
  option: EChartsCoreOption;
}

export function scoreTrendOption(points: AgentScoreHistoryPoint[], color = chartPalette[0]): ScoreTrendSeries {
  const ordered = [...points].sort(
    (left, right) => Date.parse(left.source_published_at) - Date.parse(right.source_published_at)
  );
  return {
    ordered,
    option: {
      color: [color],
      aria: { enabled: true, decal: { show: true } },
      grid: { top: 28, right: 20, bottom: 30, left: 44 },
      tooltip: {
        trigger: "axis",
        valueFormatter: (value: number) => (value === null || value === undefined ? "—" : value.toFixed(2))
      },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: ordered.map((point) => point.source_published_at.slice(0, 10)),
        axisLabel: { color: "#64748b", fontSize: 10 },
        axisLine: { lineStyle: { color: "#cbd5e1" } }
      },
      yAxis: {
        type: "value",
        min: 0,
        max: 100,
        axisLabel: { color: "#64748b", fontSize: 10 },
        splitLine: { lineStyle: { color: "#dce3ee" } }
      },
      series: [{
        type: "line",
        name: "综合分",
        smooth: true,
        symbolSize: 7,
        lineStyle: { width: 2.5 },
        areaStyle: { opacity: 0.08 },
        data: ordered.map((point) => point.score)
      }]
    }
  };
}
