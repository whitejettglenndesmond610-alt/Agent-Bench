import type { EChartsCoreOption } from "echarts/core";

import { AgentScoreHistoryPoint } from "./api";
import { abilityMetrics } from "./metrics";

// Dark-theme defaults mirroring design-tokens.css. At runtime the charts read
// the live CSS custom properties so both the dark default and the light
// opt-in theme stay readable without duplicating palettes here.
const tokenFallbacks = {
  accent: "#ffca63",
  cyan: "#6cd4e0",
  violet: "#a78bfa",
  rose: "#ef6a67",
  green: "#6bd89b",
  muted: "#8a94a6",
  line: "#232c3b",
  lineStrong: "#35425a",
  surface: "#131822",
  bg: "#0b0e14"
} as const;

function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined" || typeof window.getComputedStyle !== "function") return fallback;
  const value = window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

// Mirrors design-tokens.css: --accent, --cyan, --violet, --rose, --green.
export const chartPalette = [
  tokenFallbacks.accent,
  tokenFallbacks.cyan,
  tokenFallbacks.violet,
  tokenFallbacks.rose,
  tokenFallbacks.green
];

function themedPalette(): string[] {
  return [
    cssVar("--accent", chartPalette[0]),
    cssVar("--cyan", chartPalette[1]),
    cssVar("--violet", chartPalette[2]),
    cssVar("--rose", chartPalette[3]),
    cssVar("--green", chartPalette[4])
  ];
}

function chartInk() {
  return {
    muted: cssVar("--muted", tokenFallbacks.muted),
    line: cssVar("--line", tokenFallbacks.line),
    lineStrong: cssVar("--line-strong", tokenFallbacks.lineStrong),
    surface: cssVar("--surface", tokenFallbacks.surface),
    bg: cssVar("--bg", tokenFallbacks.bg)
  };
}

export interface RadarEntry {
  name: string;
  values: Array<number | null>;
}

export function abilityRadarOption(entries: RadarEntry[]): EChartsCoreOption {
  const ink = chartInk();
  return {
    color: themedPalette(),
    aria: { enabled: true, decal: { show: true } },
    tooltip: { trigger: "item" },
    legend: {
      bottom: 0,
      textStyle: { color: ink.muted, fontSize: 10 },
      data: entries.map((entry) => entry.name)
    },
    radar: {
      center: ["50%", "43%"],
      radius: "62%",
      splitNumber: 5,
      indicator: abilityMetrics.map((ability) => ({ name: ability.label, max: 100, min: 0 })),
      axisName: { color: ink.muted, fontSize: 10 },
      splitArea: { areaStyle: { color: [ink.surface, ink.bg] } },
      splitLine: { lineStyle: { color: ink.line } },
      axisLine: { lineStyle: { color: ink.lineStrong } }
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

export function scoreTrendOption(points: AgentScoreHistoryPoint[], color?: string): ScoreTrendSeries {
  const ordered = [...points].sort(
    (left, right) => Date.parse(left.source_published_at) - Date.parse(right.source_published_at)
  );
  const ink = chartInk();
  const lineColor = color ?? themedPalette()[0];
  return {
    ordered,
    option: {
      color: [lineColor],
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
        axisLabel: { color: ink.muted, fontSize: 10 },
        axisLine: { lineStyle: { color: ink.lineStrong } }
      },
      yAxis: {
        type: "value",
        min: 0,
        max: 100,
        axisLabel: { color: ink.muted, fontSize: 10 },
        splitLine: { lineStyle: { color: ink.line } }
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

// Minimal axis-less line for KPI cards. Density over decoration: no grid,
// no labels, just the trend shape.
export function sparklineOption(values: number[], color?: string): EChartsCoreOption {
  const lineColor = color ?? themedPalette()[0];
  return {
    color: [lineColor],
    animation: false,
    grid: { top: 2, right: 2, bottom: 2, left: 2 },
    xAxis: { type: "category", show: false, boundaryGap: false, data: values.map((_, index) => index) },
    yAxis: { type: "value", show: false, min: (value: { min: number }) => value.min, max: (value: { max: number }) => value.max },
    series: [{
      type: "line",
      smooth: true,
      symbol: "none",
      lineStyle: { width: 1.6 },
      areaStyle: { opacity: 0.14 },
      data: values
    }]
  };
}
