import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { fetchAgentLeaderboardDetail, fetchLeaderboardHistory, fetchLeaderboardSnapshot } from "./api";
import { scoreTrendOption } from "./chartOptions";
import { EmptyState, LoadingState, PageHeader, StatusPill } from "./components";
import { EChart } from "./EChart";
import { formatScoreWithCi } from "./metrics";
import { downloadJson, downloadMarkdown, formatUserDate, snapshotMarkdown } from "./userProduct";
import "./leaderboard-history.css";

export function LeaderboardHistoryPage() {
  const historyQuery = useQuery({ queryKey: ["leaderboard-history"], queryFn: fetchLeaderboardHistory });
  return (
    <main className="workspace leaderboard-history-page">
      <Link className="back-link" to="/leaderboard">← 返回当前排行榜</Link>
      <PageHeader
        title="榜单历史"
        meta={<span>按发布时间查看正式榜单快照，每个快照保留当时的排名和协议数据。</span>}
      />
      {historyQuery.isPending ? <LoadingState label="正在读取榜单发布时间线" /> : historyQuery.error ? <div className="table-state is-error" role="alert">{historyQuery.error.message}</div> : historyQuery.data?.length ? (
        <ol className="leaderboard-timeline stagger-in">{historyQuery.data.map((snapshot, index) => (
          <li key={snapshot.id}>
            <div><i /><span>{String(index + 1).padStart(2, "0")}</span></div>
            <article><header><div><h2><Link to={`/leaderboard/history/${snapshot.id}`}>{snapshot.run_name}</Link></h2><time dateTime={snapshot.published_at}>{formatUserDate(snapshot.published_at, true)}</time></div>{snapshot.is_current && <StatusPill tone="success">当前快照</StatusPill>}</header><footer><span>{snapshot.agent_count} 个 Agent</span><code>SNAPSHOT {snapshot.id.slice(0, 12)}</code><Link to={`/leaderboard/history/${snapshot.id}`}>查看排名 →</Link></footer></article>
          </li>
        ))}</ol>
      ) : <EmptyState title="还没有榜单发布记录" description="正式榜单发布后会出现在时间线中。" />}
    </main>
  );
}

function AgentScoreTrendPanel({ agentId, agentName, onClose }: { agentId: string; agentName: string; onClose: () => void }) {
  const detailQuery = useQuery({
    queryKey: ["leaderboard-agent-detail", agentId],
    queryFn: () => fetchAgentLeaderboardDetail(agentId),
    retry: false
  });
  const points = detailQuery.data?.score_history.overall ?? [];
  const trendOption = useMemo(
    () => scoreTrendOption(detailQuery.data?.score_history.overall ?? []).option,
    [detailQuery.data]
  );
  return (
    <section className="snapshot-trend-panel" aria-label={`${agentName} 综合分趋势`}>
      <header>
        <div><p className="eyebrow">SCORE TREND</p><h2>{agentName} 综合分趋势</h2><p>同一 Agent 跨榜单快照的综合分，按发布时间排序。</p></div>
        <button type="button" aria-label="关闭趋势" onClick={onClose}>×</button>
      </header>
      {detailQuery.isPending ? <LoadingState label="正在读取分数趋势" />
        : detailQuery.error ? <div className="table-state is-error" role="alert">{detailQuery.error.message}</div>
        : points.length ? (
          <EChart
            option={trendOption}
            className="snapshot-trend-chart"
            ariaLabel={`${agentName} 跨快照综合分趋势折线图，0 到 100 分`}
            fallbackHeight={300}
          />
        ) : <div className="user-section-state">该 Agent 尚无跨快照综合分记录。</div>}
    </section>
  );
}

export function LeaderboardSnapshotPage() {
  const { snapshotId = "" } = useParams();
  const [trendAgent, setTrendAgent] = useState<{ id: string; name: string } | null>(null);
  const snapshotQuery = useQuery({
    queryKey: ["leaderboard-snapshot", snapshotId],
    queryFn: () => fetchLeaderboardSnapshot(snapshotId),
    enabled: Boolean(snapshotId),
    retry: false
  });
  if (snapshotQuery.isPending) return <main className="workspace"><LoadingState label="正在读取榜单快照" /></main>;
  if (snapshotQuery.error || !snapshotQuery.data) return <main className="workspace"><div className="table-state is-error" role="alert">{snapshotQuery.error?.message ?? "榜单快照不存在"}</div></main>;
  const snapshot = snapshotQuery.data;
  return (
    <main className="workspace leaderboard-snapshot-page">
      <Link className="back-link" to="/leaderboard/history">← 返回榜单历史</Link>
      <PageHeader
        title={snapshot.run_name}
        meta={<span>{formatUserDate(snapshot.published_at, true)} 正式发布 · {snapshot.rankings.length} 个 Agent</span>}
        actions={<div className="snapshot-actions"><button className="button is-ghost" type="button" onClick={() => downloadMarkdown(snapshot.run_name, snapshotMarkdown(snapshot))}>导出 Markdown</button><button className="button is-ghost" type="button" onClick={() => downloadJson(snapshot.run_name, snapshot)}>导出 JSON</button></div>}
      />
      <aside className="snapshot-protocol"><span>数据集组合指纹</span><code>{snapshot.dataset_bundle_hash}</code><small>快照是只读发布记录，排名不会随当前榜单变化。</small></aside>
      <div className="table-frame snapshot-ranking-table"><table><thead><tr><th>排名 / Agent</th><th>综合分</th><th>回答质量</th><th>引用质量</th><th>成功率</th><th>异常率</th><th>pass@1 / pass^k</th><th>延迟 P50 / P95</th><th>公开详情</th></tr></thead><tbody>{snapshot.rankings.map((ranking) => (
        <tr key={ranking.agent_version_id}><td><strong>#{String(ranking.rank).padStart(2, "0")} <Link to={`/plaza/${ranking.agent_id}`}>{ranking.agent_name}</Link></strong><small>{ranking.provider} · V{ranking.version}</small></td><td>{formatScoreWithCi(ranking.overall_score, ranking.ci_low, ranking.ci_high)}</td><td>{ranking.answer_quality.toFixed(2)}</td><td>{ranking.citation_quality.toFixed(2)}</td><td>{ranking.success_rate.toFixed(2)}%</td><td>{ranking.anomaly_rate.toFixed(2)}%</td><td>{ranking.pass_at_1 === null || ranking.pass_at_1 === undefined ? "--" : `${ranking.pass_at_1.toFixed(2)}%`} / {ranking.pass_pow_k === null || ranking.pass_pow_k === undefined ? "--" : `${ranking.pass_pow_k.toFixed(2)}%`}</td><td>{ranking.latency_p50_ms ?? "--"} / {ranking.latency_p95_ms ?? "--"} ms</td><td><button type="button" className="row-link" onClick={() => setTrendAgent({ id: ranking.agent_id, name: ranking.agent_name })}>分数趋势</button><Link className="row-link" to={`/plaza/${ranking.agent_id}`}>查看趋势</Link></td></tr>
      ))}</tbody></table></div>
      {trendAgent && <AgentScoreTrendPanel agentId={trendAgent.id} agentName={trendAgent.name} onClose={() => setTrendAgent(null)} />}
    </main>
  );
}
