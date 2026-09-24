import { useQuery } from "@tanstack/react-query";

import {
  fetchLeaderboardHistory,
  fetchLeaderboardSnapshot,
  GlobalLeaderboardMetric,
  LeaderboardSnapshot
} from "./api";

export interface LeaderboardDelta {
  /** 名次变化：正数 = 较上一快照上升。仅综合分可比较（历史快照只保留综合名次）。 */
  rankDelta: number | null;
  /** 分数变化：当前分 - 上一快照分。 */
  scoreDelta: number | null;
}

/**
 * 当前榜单之前最近一个已发布快照。历史不可用或发布次数少于两次时返回 null，
 * 调用方据此隐藏 delta 标记。
 */
export function usePreviousLeaderboardSnapshot(): LeaderboardSnapshot | null {
  const historyQuery = useQuery({
    queryKey: ["leaderboard-history"],
    queryFn: fetchLeaderboardHistory,
    retry: false
  });
  const history = Array.isArray(historyQuery.data) ? historyQuery.data : [];
  const ordered = [...history].sort(
    (left, right) => Date.parse(right.published_at) - Date.parse(left.published_at)
  );
  const previousId = ordered.length >= 2 ? ordered[1].id : null;
  const snapshotQuery = useQuery({
    queryKey: ["leaderboard-snapshot", previousId ?? "none"],
    queryFn: () => fetchLeaderboardSnapshot(previousId as string),
    enabled: Boolean(previousId),
    retry: false
  });
  return snapshotQuery.data ?? null;
}

export function deltaForMetric(
  snapshot: LeaderboardSnapshot | null,
  agentId: string,
  metric: GlobalLeaderboardMetric,
  current: { rank: number | null; score: number | null }
): LeaderboardDelta | null {
  if (!snapshot || current.score === null) return null;
  const previous = snapshot.rankings.find((ranking) => ranking.agent_id === agentId);
  if (!previous) return null;
  const previousScore = metric === "overall"
    ? previous.overall_score
    : previous.ability_scores[metric] ?? null;
  if (previousScore === null || previousScore === undefined) return null;
  return {
    rankDelta: metric === "overall" && current.rank !== null ? previous.rank - current.rank : null,
    scoreDelta: current.score - previousScore
  };
}
