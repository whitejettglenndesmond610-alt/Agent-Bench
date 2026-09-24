import { LeaderboardDelta } from "./leaderboardDelta";

/** 紧凑 delta 标记：↑/↓ 名次箭头 + 带符号分差。算不出（delta 为 null）时不渲染。 */
export function DeltaMark({ delta, className = "" }: { delta: LeaderboardDelta | null; className?: string }) {
  if (!delta) return null;
  const { rankDelta, scoreDelta } = delta;
  const scoreText = scoreDelta === null
    ? null
    : `${scoreDelta > 0 ? "+" : scoreDelta < 0 ? "-" : "±"}${Math.abs(scoreDelta).toFixed(2)}`;
  const rankMoved = rankDelta !== null && rankDelta !== 0;
  const improved = rankMoved ? (rankDelta ?? 0) > 0 : (scoreDelta ?? 0) > 0.004;
  const declined = rankMoved ? (rankDelta ?? 0) < 0 : (scoreDelta ?? 0) < -0.004;
  const tone = improved ? "is-up" : declined ? "is-down" : "is-flat";
  const labelParts = [
    rankDelta !== null && rankDelta !== 0 ? `名次${rankDelta > 0 ? "上升" : "下降"} ${Math.abs(rankDelta)}` : null,
    scoreText ? `分数 ${scoreText}` : null
  ].filter(Boolean).join("，");
  return (
    <span
      className={`delta-mark ${tone} ${className}`.replace(/\s+/g, " ").trim()}
      role="img"
      aria-label={`较上一发布快照：${labelParts || "持平"}`}
      title="较上一发布快照"
    >
      {rankDelta !== null && rankDelta !== 0 && <i aria-hidden="true">{rankDelta > 0 ? `↑${rankDelta}` : `↓${Math.abs(rankDelta)}`}</i>}
      {scoreText}
    </span>
  );
}
