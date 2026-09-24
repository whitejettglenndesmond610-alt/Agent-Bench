import { useQuery } from "@tanstack/react-query";
import { CSSProperties, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  AgentLeaderboardDetail,
  AgentScoreHistoryPoint,
  fetchAgentLeaderboardDetail,
  fetchGlobalLeaderboardOverview,
  fetchWorkspaceAgents,
  GlobalLeaderboardMetric,
  GlobalOverviewAgent,
  WorkspaceAgent
} from "./api";
import { EmptyState, LoadingState, PageHeader, StatusPill } from "./components";
import { leaderboardMetrics, metricByKey } from "./metrics";
import { FavoriteAgentButton, formatUserDate, userQueryKeys } from "./userProduct";
import "./agent-plaza.css";

function PlazaCard({
  userId,
  agent,
  result
}: {
  userId: string;
  agent: WorkspaceAgent;
  result?: GlobalOverviewAgent;
}) {
  const overall = result?.metrics.overall;
  return (
    <article className="plaza-agent-card">
      <header>
        <i>{agent.name.slice(0, 2).toUpperCase()}</i>
        <div><Link to={`/plaza/${agent.agent_id}`}>{agent.name}</Link><span>{agent.provider} · 活跃版本 V{agent.version}</span></div>
        <FavoriteAgentButton userId={userId} agent={agent} />
      </header>
      <p>{agent.description || "该 Agent 暂未提供公开描述。"}</p>
      {(overall?.score != null || overall?.latency_p50_ms != null || overall?.success_rate != null) && (
        <div className="plaza-agent-metrics">
          <span><small>综合分</small><b>{overall?.score?.toFixed(2) ?? "--"}</b></span>
          <span><small>P50 延迟</small><b>{overall?.latency_p50_ms == null ? "--" : `${overall.latency_p50_ms} ms`}</b></span>
          <span><small>成功率</small><b>{overall?.success_rate == null ? "--" : `${overall.success_rate.toFixed(1)}%`}</b></span>
        </div>
      )}
      <div className="plaza-agent-badges">
        <StatusPill tone={agent.supports_citations ? "info" : "neutral"}>{agent.supports_citations ? "支持引用" : "纯文本回答"}</StatusPill>
        <StatusPill tone={overall?.score == null ? "neutral" : "success"}>{overall?.score == null ? "暂无正式综合成绩" : "已有正式成绩"}</StatusPill>
      </div>
      <section className="plaza-score-summary">
        <div><span>综合分</span><strong>{overall?.score?.toFixed(2) ?? "--"}</strong><small>{overall?.rank == null ? "未排名" : `正式排名 #${overall.rank}`}</small></div>
        <dl>{leaderboardMetrics.slice(1).map((metric) => (
          <div key={metric.key}><dt>{metric.shortLabel}</dt><dd>{result?.metrics[metric.key].score?.toFixed(1) ?? "--"}</dd></div>
        ))}</dl>
      </section>
      <footer>
        <Link to={`/workspace?agentVersion=${encodeURIComponent(agent.agent_version_id)}`}>在线运行</Link>
        <Link to={`/compare?agentVersion=${encodeURIComponent(agent.agent_version_id)}`}>加入对比</Link>
        <Link to={`/plaza/${agent.agent_id}`}>查看详情</Link>
      </footer>
    </article>
  );
}

export function AgentPlazaPage({ userId }: { userId: string }) {
  const [search, setSearch] = useState("");
  const [provider, setProvider] = useState("all");
  const [favorite, setFavorite] = useState(false);
  const [citations, setCitations] = useState(false);
  const [evaluated, setEvaluated] = useState(false);
  const agentsQuery = useQuery({ queryKey: userQueryKeys.agents(userId), queryFn: fetchWorkspaceAgents });
  const overviewQuery = useQuery({
    queryKey: ["leaderboard-global-overview"],
    queryFn: fetchGlobalLeaderboardOverview
  });

  const agents = (agentsQuery.data ?? []).filter((agent) => agent.visibility !== "private");
  const overviewById = new Map((overviewQuery.data?.agents ?? []).map((agent) => [agent.agent_id, agent]));
  const providers = [...new Set(agents.map((agent) => agent.provider))].sort((left, right) => left.localeCompare(right, "zh-CN"));
  const query = search.trim().toLocaleLowerCase();
  const filtered = agents.filter((agent) => {
    const official = overviewById.get(agent.agent_id);
    return (!query || `${agent.name} ${agent.provider} ${agent.description ?? ""}`.toLocaleLowerCase().includes(query))
      && (provider === "all" || agent.provider === provider)
      && (!favorite || agent.is_favorite)
      && (!citations || agent.supports_citations)
      && (!evaluated || official?.metrics.overall.score != null);
  });

  return (
    <main className="workspace agent-plaza-page">
      <PageHeader
        title="Agent 广场"
        meta={<span>结合活跃服务版本与正式公开成绩，找到适合当前任务的 Agent。</span>}
        actions={<Link className="button" to="/workspace">直接开始对话</Link>}
      />
      <section className="plaza-tools" aria-label="Agent 筛选">
        <label><span>搜索</span><input className="control" type="search" aria-label="搜索 Agent" value={search} placeholder="名称、提供方或能力描述" onChange={(event) => setSearch(event.target.value)} /></label>
        <label><span>提供方</span><select className="control" aria-label="筛选提供方" value={provider} onChange={(event) => setProvider(event.target.value)}><option value="all">全部提供方</option>{providers.map((item) => <option key={item}>{item}</option>)}</select></label>
        <label className="plaza-check"><input type="checkbox" checked={favorite} onChange={(event) => setFavorite(event.target.checked)} />仅看收藏</label>
        <label className="plaza-check"><input type="checkbox" checked={citations} onChange={(event) => setCitations(event.target.checked)} />支持引用</label>
        <label className="plaza-check"><input type="checkbox" checked={evaluated} onChange={(event) => setEvaluated(event.target.checked)} />已有评测</label>
        <strong>{filtered.length} / {agents.length}</strong>
      </section>
      {agentsQuery.isPending ? <LoadingState label="正在读取 Agent 广场" /> : agentsQuery.error ? (
        <div className="table-state is-error" role="alert">{agentsQuery.error.message}</div>
      ) : filtered.length ? (
        <section className="plaza-agent-grid">
          {filtered.map((agent) => <PlazaCard key={agent.agent_version_id} userId={userId} agent={agent} result={overviewById.get(agent.agent_id)} />)}
        </section>
      ) : (
        <EmptyState title="没有符合筛选条件的 Agent" description="清除部分筛选条件后再试。" />
      )}
      {overviewQuery.error && <p className="plaza-score-warning" role="alert">正式成绩暂时无法读取，Agent 仍可正常运行：{overviewQuery.error.message}</p>}
    </main>
  );
}

function historyCoordinates(points: AgentScoreHistoryPoint[]) {
  const ordered = [...points].sort((left, right) => Date.parse(left.source_published_at) - Date.parse(right.source_published_at));
  if (!ordered.length) return { ordered, coordinates: [] as string[], series: [] as Array<{ protocol: string; current: boolean; coordinates: string }> };
  const width = 680;
  const height = 190;
  const coordinates = ordered.map((point, index) => {
    const x = ordered.length === 1 ? width / 2 : 24 + index * ((width - 48) / (ordered.length - 1));
    const y = 16 + (100 - point.score) * ((height - 32) / 100);
    return `${x},${y}`;
  });
  const series = Array.from(new Set(ordered.map((point) => point.protocol_hash))).map((protocol) => ({
    protocol,
    current: ordered.some((point) => point.protocol_hash === protocol && point.protocol_matches_current),
    coordinates: ordered.flatMap((point, index) => point.protocol_hash === protocol ? [coordinates[index]] : []).join(" ")
  }));
  return { ordered, coordinates, series };
}

function ScoreTrend({ detail }: { detail: AgentLeaderboardDetail }) {
  const available = leaderboardMetrics.filter((metric) => detail.score_history[metric.key]?.length);
  const [metric, setMetric] = useState<GlobalLeaderboardMetric>(available[0]?.key ?? "overall");
  const { ordered, coordinates, series } = historyCoordinates(detail.score_history[metric] ?? []);
  const protocols = new Set(ordered.map((point) => point.protocol_hash));
  return (
    <section className="plaza-trend-panel">
      <header><div><p className="eyebrow">OFFICIAL SCORE HISTORY</p><h2>正式成绩趋势</h2></div><label><span className="sr-only">趋势指标</span><select className="control" aria-label="趋势指标" value={metric} onChange={(event) => setMetric(event.target.value as GlobalLeaderboardMetric)}>{leaderboardMetrics.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}</select></label></header>
      {ordered.length ? <>
        <div className="score-trend-chart">
          <svg viewBox="0 0 680 190" role="img" aria-label={`${metricByKey[metric].label}正式分数趋势，0 到 100 分`}>
            <line x1="24" x2="656" y1="16" y2="16" /><line x1="24" x2="656" y1="95" y2="95" /><line x1="24" x2="656" y1="174" y2="174" />
            {series.map((item) => <polyline className={item.current ? "is-current-protocol" : "is-historical-protocol"} key={item.protocol} points={item.coordinates} style={{ "--trend-color": metricByKey[metric].color } as CSSProperties} />)}
            {coordinates.map((coordinate, index) => { const [cx, cy] = coordinate.split(","); return <circle className={ordered[index].protocol_matches_current ? "is-current-protocol" : "is-historical-protocol"} key={ordered[index].source_snapshot_id} cx={cx} cy={cy} r={ordered[index].is_current_source ? 6 : 4} />; })}
          </svg>
          <span className="trend-axis is-top">100</span><span className="trend-axis is-middle">50</span><span className="trend-axis is-bottom">0</span>
        </div>
        <p className="trend-protocol-note">共 {ordered.length} 次发布 · {protocols.size} 套协议。实心大点为当前正式成绩；协议不兼容的历史点仅用于观察，不用于直接排名。</p>
        <ol className="trend-publications">{[...ordered].reverse().map((point) => (
          <li key={`${point.source_snapshot_id}-${point.source_profile_key}`}><span>{formatUserDate(point.source_published_at, true)}</span><strong>{point.score.toFixed(2)}</strong><small>V{point.version} · {point.source_run_name}</small><b>{point.protocol_matches_current ? "当前协议" : "历史协议"}</b></li>
        ))}</ol>
      </> : <div className="user-section-state">该指标尚无正式发布记录。</div>}
    </section>
  );
}

export function AgentPlazaDetailPage({ userId }: { userId: string }) {
  const { agentId = "" } = useParams();
  const detailQuery = useQuery({
    queryKey: ["leaderboard-agent-detail", agentId],
    queryFn: () => fetchAgentLeaderboardDetail(agentId),
    enabled: Boolean(agentId),
    retry: false
  });
  const agentsQuery = useQuery({ queryKey: userQueryKeys.agents(userId), queryFn: fetchWorkspaceAgents });

  if (detailQuery.isPending) return <main className="workspace"><LoadingState label="正在读取 Agent 公开档案" /></main>;
  if (detailQuery.error || !detailQuery.data) return <main className="workspace"><div className="table-state is-error" role="alert">{detailQuery.error?.message ?? "Agent 不存在或不可用"}</div></main>;

  const detail = detailQuery.data;
  const active = agentsQuery.data?.find((agent) => agent.visibility !== "private" && agent.agent_id === detail.agent_id);
  const quality = detail.metrics.overall.score !== null
    ? detail.metrics.overall
    : leaderboardMetrics.map((metric) => detail.metrics[metric.key]).find((metric) => metric.score !== null);
  const versions = Array.from(new Map(
    leaderboardMetrics.flatMap((metric) => detail.score_history[metric.key] ?? []).map((point) => [point.agent_version_id, point])
  ).values()).sort((left, right) => right.version - left.version);
  const sources = Array.from(new Map(
    leaderboardMetrics.flatMap((metric) => detail.score_history[metric.key] ?? []).map((point) => [point.source_snapshot_id, point])
  ).values()).sort((left, right) => Date.parse(right.source_published_at) - Date.parse(left.source_published_at));

  return (
    <main className="workspace plaza-detail-page">
      <Link className="back-link" to="/plaza">← 返回 Agent 广场</Link>
      <PageHeader
        className="plaza-detail-header"
        title={<><i className="plaza-detail-avatar" aria-hidden="true">{detail.agent_name.slice(0, 2).toUpperCase()}</i>{detail.agent_name}</>}
        meta={<span>{detail.provider}{active ? ` · 活跃版本 V${active.version}` : detail.version ? ` · 公开成绩版本 V${detail.version}` : ""}</span>}
        actions={active && <>
          <FavoriteAgentButton userId={userId} agent={active} className="button is-ghost" />
          <Link className="button" to={`/workspace?agentVersion=${active.agent_version_id}`}>在线运行</Link>
          <Link className="button is-ghost" to={`/compare?agentVersion=${active.agent_version_id}`}>加入对比</Link>
        </>}
      />
      <p className="plaza-detail-description">{detail.description || "该 Agent 暂未提供公开描述。"}</p>
      <section className="plaza-public-metrics">{leaderboardMetrics.map((metric) => {
        const result = detail.metrics[metric.key];
        return <article key={metric.key}><header><i style={{ background: metric.color }} /><span>{metric.label}</span></header><strong>{result.score?.toFixed(2) ?? "--"}</strong><p>{result.rank == null ? "暂无正式排名" : `正式排名 #${result.rank}`}</p><small>{result.source_published_at ? `${formatUserDate(result.source_published_at)} 发布` : "尚未发布"}</small></article>;
      })}</section>
      <section className="plaza-quality-panel">
        <header><div><p className="eyebrow">PUBLIC OPERATIONAL QUALITY</p><h2>公开运行质量</h2></div><small>取最近可用正式结果</small></header>
        <dl>
          <div><dt>成功率</dt><dd>{quality?.success_rate == null ? "--" : `${quality.success_rate.toFixed(2)}%`}</dd></div>
          <div><dt>异常率</dt><dd>{quality?.anomaly_rate == null ? "--" : `${quality.anomaly_rate.toFixed(2)}%`}</dd></div>
          <div><dt>HTTP 调用</dt><dd>{quality?.http_call_count ?? "--"}</dd></div>
          <div><dt>Benchmark</dt><dd>{quality?.benchmark_count ?? "--"}</dd></div>
          <div><dt>P50 延迟</dt><dd>{quality?.latency_p50_ms == null ? "--" : `${quality.latency_p50_ms} ms`}</dd></div>
          <div><dt>P95 延迟</dt><dd>{quality?.latency_p95_ms == null ? "--" : `${quality.latency_p95_ms} ms`}</dd></div>
        </dl>
      </section>
      <ScoreTrend detail={detail} />
      <section className="plaza-publication-grid">
        <article><header><p className="eyebrow">VERSION TRANSITIONS</p><h2>成绩版本沿革</h2></header>{versions.length ? <ol>{versions.map((version) => <li key={version.agent_version_id}><strong>V{version.version}</strong><span>{formatUserDate(version.source_published_at, true)}</span><small>{version.is_current_source ? "当前成绩来源" : "历史成绩来源"}</small></li>)}</ol> : <p>暂无已发布版本。</p>}</article>
        <article><header><p className="eyebrow">PUBLICATION SOURCES</p><h2>发布来源</h2></header>{sources.length ? <ol>{sources.slice(0, 12).map((source) => <li key={source.source_snapshot_id}><strong>{source.source_run_name}</strong><span>{source.source_profile_key}</span><small>{formatUserDate(source.source_published_at, true)} · {source.benchmark_count} Benchmarks</small></li>)}</ol> : <p>暂无正式发布来源。</p>}</article>
      </section>
    </main>
  );
}
