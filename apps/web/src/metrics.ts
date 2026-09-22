import { Ability, GlobalLeaderboardMetric } from "./api";

export interface LeaderboardMetricDefinition {
  key: GlobalLeaderboardMetric;
  label: string;
  shortLabel: string;
  boardLabel: string;
  color: string;
  tone: string;
}

export const leaderboardMetrics: LeaderboardMetricDefinition[] = [
  { key: "overall", label: "综合分", shortLabel: "综合", boardLabel: "综合榜", color: "#ffca63", tone: "amber" },
  { key: "reasoning", label: "通用问答与推理", shortLabel: "推理", boardLabel: "推理榜", color: "#e8ad4f", tone: "amber" },
  { key: "retrieval", label: "信息检索", shortLabel: "检索", boardLabel: "检索榜", color: "#63d9d0", tone: "cyan" },
  { key: "data_reasoning", label: "数据与数值推理", shortLabel: "数据推理", boardLabel: "数据推理榜", color: "#a999ff", tone: "violet" },
  { key: "instruction_following", label: "指令遵循", shortLabel: "指令遵循", boardLabel: "指令遵循榜", color: "#6bd89b", tone: "green" },
  { key: "long_context", label: "长上下文", shortLabel: "长上下文", boardLabel: "长上下文榜", color: "#ef8683", tone: "rose" }
];

export const abilityMetrics = leaderboardMetrics.filter(
  (metric): metric is LeaderboardMetricDefinition & { key: Ability } => metric.key !== "overall"
);

export const metricByKey = Object.fromEntries(
  leaderboardMetrics.map((metric) => [metric.key, metric])
) as Record<GlobalLeaderboardMetric, LeaderboardMetricDefinition>;

export function profileKeyForMetric(metric: GlobalLeaderboardMetric): string {
  return metric === "overall" ? "unified-v3" : `unified-v3-${metric}`;
}

export function formatScoreWithCi(
  score: number,
  ciLow?: number | null,
  ciHigh?: number | null
): string {
  if (ciLow === null || ciLow === undefined || ciHigh === null || ciHigh === undefined) {
    return score.toFixed(2);
  }
  return `${score.toFixed(2)} (${ciLow.toFixed(2)}–${ciHigh.toFixed(2)})`;
}

export const CALIBRATION_MIN_RELIABLE_SAMPLE = 30;
export const CALIBRATION_JUDGE_PASS_THRESHOLD = 60;

export function formatCalibrationRate(value: number | null): string {
  if (value === null) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

export function formatCalibrationKappa(value: number | null): string {
  if (value === null) return "—";
  return value.toFixed(2);
}

export function formatCostUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `$${value.toPrecision(4).replace(/\.?0+$/, "")}`;
}
