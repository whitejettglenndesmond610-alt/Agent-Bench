import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { fetchLeaderboardHistory, fetchLeaderboardSnapshot } from "./api";
import { LoadingState } from "./components";
import { downloadJson, downloadMarkdown, formatUserDate, snapshotMarkdown } from "./userProduct";

export function LeaderboardHistoryPage() {
  const historyQuery = useQuery({ queryKey: ["leaderboard-history"], queryFn: fetchLeaderboardHistory });
  return (
    <main className="workspace leaderboard-history-page">
      <Link className="back-link" to="/leaderboard">← 返回当前排行榜</Link>
      <header className="section-header"><div><p className="eyebrow">PUBLICATION TIMELINE</p><h1>榜单历史</h1><p>按发布时间查看正式榜单快照。每个快照保留当时发布的排名和协议数据。</p></div></header>
      {historyQuery.isPending ? <LoadingState label="正在读取榜单发布时间线" /> : historyQuery.error ? <div className="table-state is-error" role="alert">{historyQuery.error.message}</div> : historyQuery.data?.length ? (
        <ol className="leaderboard-timeline">{historyQuery.data.map((snapshot, index) => (
          <li key={snapshot.id}>
            <div><i /><span>{String(index + 1).padStart(2, "0")}</span></div>
            <article><header><div><h2><Link to={`/leaderboard/history/${snapshot.id}`}>{snapshot.run_name}</Link></h2><time dateTime={snapshot.published_at}>{formatUserDate(snapshot.published_at, true)}</time></div>{snapshot.is_current && <b>当前快照</b>}</header><footer><span>{snapshot.agent_count} 个 Agent</span><code>SNAPSHOT {snapshot.id.slice(0, 12)}</code><Link to={`/leaderboard/history/${snapshot.id}`}>查看排名 →</Link></footer></article>
          </li>
        ))}</ol>
      ) : <section className="empty-state"><span>NO PUBLICATION</span><h2>还没有榜单发布记录</h2><p>正式榜单发布后会出现在时间线中。</p></section>}
    </main>
  );
}

export function LeaderboardSnapshotPage() {
  const { snapshotId = "" } = useParams();
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
      <header className="section-header"><div><p className="eyebrow">PUBLISHED SNAPSHOT</p><h1>{snapshot.run_name}</h1><p>{formatUserDate(snapshot.published_at, true)} 正式发布 · {snapshot.rankings.length} 个 Agent</p></div><div className="snapshot-actions"><button className="button is-ghost" type="button" onClick={() => downloadMarkdown(snapshot.run_name, snapshotMarkdown(snapshot))}>导出 Markdown</button><button className="button is-ghost" type="button" onClick={() => downloadJson(snapshot.run_name, snapshot)}>导出 JSON</button></div></header>
      <aside className="snapshot-protocol"><span>数据集组合指纹</span><code>{snapshot.dataset_bundle_hash}</code><small>快照是只读发布记录，排名不会随当前榜单变化。</small></aside>
      <div className="table-frame snapshot-ranking-table"><table><thead><tr><th>排名 / Agent</th><th>综合分</th><th>回答质量</th><th>引用质量</th><th>成功率</th><th>异常率</th><th>延迟 P50 / P95</th><th>公开详情</th></tr></thead><tbody>{snapshot.rankings.map((ranking) => (
        <tr key={ranking.agent_version_id}><td><strong>#{String(ranking.rank).padStart(2, "0")} <Link to={`/plaza/${ranking.agent_id}`}>{ranking.agent_name}</Link></strong><small>{ranking.provider} · V{ranking.version}</small></td><td>{ranking.overall_score.toFixed(2)}</td><td>{ranking.answer_quality.toFixed(2)}</td><td>{ranking.citation_quality.toFixed(2)}</td><td>{ranking.success_rate.toFixed(2)}%</td><td>{ranking.anomaly_rate.toFixed(2)}%</td><td>{ranking.latency_p50_ms ?? "--"} / {ranking.latency_p95_ms ?? "--"} ms</td><td><Link className="row-link" to={`/plaza/${ranking.agent_id}`}>查看趋势</Link></td></tr>
      ))}</tbody></table></div>
    </main>
  );
}
