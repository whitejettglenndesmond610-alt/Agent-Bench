import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  AgentEvaluationMetricStatus,
  fetchGlobalLeaderboardOverview,
  GlobalLeaderboardMetric,
  GlobalOverviewAgent,
  GlobalOverviewMetric
} from "./api";
import { EmptyState, Input, LoadingState, PageHeader, Select, Table, Tabs } from "./components";
import { abilityRadarOption } from "./chartOptions";
import { EChart } from "./EChart";
import { abilityMetrics, leaderboardMetrics, metricByKey, profileKeyForMetric, formatCostUsd, formatScoreWithCi } from "./metrics";
import "./leaderboard.css";

type BoardView = "overview" | GlobalLeaderboardMetric;
type SortDirection = "asc" | "desc";

const tabs: Array<{ key: BoardView; label: string; hint: string }> = [
  { key: "overview", label: "能力总览", hint: "OVERVIEW" },
  ...leaderboardMetrics.map((metric) => ({ key: metric.key as BoardView, label: metric.boardLabel, hint: metric.key.toUpperCase() }))
];

const statusLabels: Record<AgentEvaluationMetricStatus, string> = {
  not_evaluated: "未评测",
  queued: "排队中",
  running: "评测中",
  evaluated: "当前有效",
  needs_retest: "待复测",
  failed: "评测失败",
  invalid: "配置无效"
};

function formatPublished(value: string | null, includeYear = true) {
  if (!value) return "尚未发布";
  return new Intl.DateTimeFormat("zh-CN", {
    ...(includeYear ? { year: "numeric" } : {}),
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function hasResult(agent: GlobalOverviewAgent, metric?: GlobalLeaderboardMetric) {
  if (metric) return agent.metrics[metric].rank !== null;
  return leaderboardMetrics.some(({ key }) => agent.metrics[key].rank !== null);
}

function latestResult(agent: GlobalOverviewAgent, preferred: GlobalLeaderboardMetric) {
  if (agent.metrics[preferred].score !== null) return agent.metrics[preferred];
  return leaderboardMetrics
    .map(({ key }) => agent.metrics[key])
    .filter((metric) => metric.score !== null)
    .sort((left, right) => Date.parse(right.source_published_at ?? "") - Date.parse(left.source_published_at ?? ""))[0];
}

function evaluationLink(agent: GlobalOverviewAgent, metric: GlobalLeaderboardMetric) {
  if (!agent.agent_version_id) return "/runs";
  return `/runs?launch=1&agentVersion=${encodeURIComponent(agent.agent_version_id)}&profile=${profileKeyForMetric(metric)}`;
}

function formatLatency(result: GlobalOverviewMetric) {
  if (result.latency_p50_ms === null || result.latency_p50_ms === undefined
    || result.latency_p95_ms === null || result.latency_p95_ms === undefined) return "—";
  return `${result.latency_p50_ms} / ${result.latency_p95_ms} ms`;
}

function formatPercent(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(digits)}%`;
}

function MetricValue({ result, withCi = false }: { result: GlobalOverviewMetric; withCi?: boolean }) {
  if (result.score === null || result.rank === null) {
    return (
      <>
        <span className="metric-empty-mark">—</span>
        <small>{statusLabels[result.status]}</small>
      </>
    );
  }
  return (
    <>
      <strong>{withCi ? formatScoreWithCi(result.score, result.ci_low, result.ci_high) : result.score.toFixed(2)}</strong>
      <small>{statusLabels[result.status]}</small>
    </>
  );
}

function MetricTableCell({
  metric,
  agent,
  isOperator
}: {
  metric: GlobalLeaderboardMetric;
  agent: GlobalOverviewAgent;
  isOperator: boolean;
}) {
  const result = agent.metrics[metric];
  const missing = result.score === null || result.rank === null;
  return (
    <td className={`metric-cell is-${result.status}`} data-numeric title={result.message ?? undefined}>
      <MetricValue result={result} />
      {missing && isOperator && result.status === "not_evaluated" && (
        <Link to={evaluationLink(agent, metric)} onClick={(event) => event.stopPropagation()}>开始评测</Link>
      )}
    </td>
  );
}

function CompareCheckbox({
  agent,
  selected,
  onToggle
}: {
  agent: GlobalOverviewAgent;
  selected: string[];
  onToggle: (agentId: string) => void;
}) {
  return (
    <td className="check-cell" onClick={(event) => event.stopPropagation()}>
      <label className="compare-check" title="选择横向对比">
        <input
          type="checkbox"
          aria-label={`选择 ${agent.agent_name}`}
          checked={selected.includes(agent.agent_id)}
          disabled={!selected.includes(agent.agent_id) && selected.length >= 5}
          onChange={() => onToggle(agent.agent_id)}
        />
        <span />
      </label>
    </td>
  );
}

function AgentIdentityCell({
  agent,
  onOpenDetail
}: {
  agent: GlobalOverviewAgent;
  onOpenDetail: () => void;
}) {
  return (
    <td className="agent-cell">
      <Link to={`/plaza/${agent.agent_id}`} onClick={(event) => event.stopPropagation()}>
        <strong>{agent.agent_name}</strong>
      </Link>
      <small>{agent.provider}{agent.version === null ? "" : ` · V${agent.version}`}</small>
      <button
        type="button"
        className="row-link"
        aria-label={`查看 ${agent.agent_name} 评测摘要`}
        onClick={(event) => {
          event.stopPropagation();
          onOpenDetail();
        }}
      >
        评测摘要
      </button>
    </td>
  );
}

function BenchmarkBreakdown({ result }: { result: GlobalOverviewMetric }) {
  if (!result.benchmark_scores.length) return <span className="benchmark-none">暂无 Benchmark 明细</span>;
  return (
    <div className="benchmark-cell">
      {result.benchmark_scores.map((benchmark) => (
        <div key={benchmark.benchmark_id}>
          <span>{benchmark.name}<small>{benchmark.language.toUpperCase()}</small></span>
          <b>{benchmark.score.toFixed(2)}</b>
        </div>
      ))}
    </div>
  );
}

function AgentDetail({
  agent,
  focusMetric,
  isOperator,
  workspaceEnabled,
  onClose
}: {
  agent: GlobalOverviewAgent;
  focusMetric: GlobalLeaderboardMetric;
  isOperator: boolean;
  workspaceEnabled: boolean;
  onClose: () => void;
}) {
  const focus = agent.metrics[focusMetric];
  const hasRadarScores = abilityMetrics.some((ability) => agent.metrics[ability.key].score !== null);
  const radarOption = useMemo(
    () => abilityRadarOption([{
      name: agent.agent_name,
      values: abilityMetrics.map((ability) => agent.metrics[ability.key].score)
    }]),
    [agent]
  );
  return (
    <div className="leaderboard-drawer-backdrop" role="presentation">
      <aside className="leaderboard-drawer" aria-label={`${agent.agent_name} 评测详情`}>
        <header>
          <div><p className="eyebrow">AGENT EVALUATION</p><h2>{agent.agent_name}</h2><span>{agent.provider}{agent.version === null ? "" : ` · V${agent.version}`}</span></div>
          <button type="button" aria-label="关闭详情" onClick={onClose}>×</button>
        </header>
        <section className="drawer-metric-grid">
          {leaderboardMetrics.map((metric) => (
            <article key={metric.key}>
              <span>{metric.shortLabel}</span>
              <div className={`metric-cell is-${agent.metrics[metric.key].status}`} title={agent.metrics[metric.key].message ?? undefined}>
                <MetricValue result={agent.metrics[metric.key]} withCi />
              </div>
            </article>
          ))}
        </section>
        {hasRadarScores && (
          <section className="drawer-section">
            <div className="drawer-section-heading"><div><p className="eyebrow">ABILITY SHAPE</p><h3>能力雷达</h3></div><span>固定范围 0—100 · 缺失能力留空不按 0 分处理</span></div>
            <EChart
              option={radarOption}
              className="drawer-radar"
              ariaLabel={`${agent.agent_name} 五项能力雷达图，坐标范围 0 到 100`}
            />
          </section>
        )}
        <section className="drawer-section">
          <div className="drawer-section-heading"><div><p className="eyebrow">FOCUS METRIC</p><h3>{metricByKey[focusMetric].boardLabel}</h3></div>{focus.score !== null && <strong>{formatScoreWithCi(focus.score, focus.ci_low, focus.ci_high)}</strong>}</div>
          <dl className="drawer-quality">
            <div><dt>成功率</dt><dd>{focus.success_rate === null ? "—" : `${focus.success_rate.toFixed(2)}%`}</dd></div>
            <div><dt>异常率</dt><dd>{focus.anomaly_rate === null ? "—" : `${focus.anomaly_rate.toFixed(2)}%`}</dd></div>
            {focus.pass_at_1 !== null && focus.pass_at_1 !== undefined && <div><dt>pass@1 单次成功率</dt><dd>{focus.pass_at_1.toFixed(2)}%</dd></div>}
            {focus.pass_pow_k !== null && focus.pass_pow_k !== undefined && <div><dt>pass^k 多次全对占比</dt><dd>{focus.pass_pow_k.toFixed(2)}%</dd></div>}
            <div><dt>HTTP 调用</dt><dd>{focus.http_call_count ?? "—"}</dd></div>
            <div><dt>Benchmark</dt><dd>{focus.benchmark_count || "—"}</dd></div>
            <div><dt>Token</dt><dd>{focus.total_tokens === null || focus.total_tokens === undefined ? "—" : new Intl.NumberFormat("zh-CN").format(focus.total_tokens)}</dd></div>
            <div><dt>输入/输出 Token</dt><dd>{focus.input_token_share === null || focus.input_token_share === undefined || focus.output_token_share === null || focus.output_token_share === undefined ? "—" : `${focus.input_token_share.toFixed(1)}% / ${focus.output_token_share.toFixed(1)}%`}</dd></div>
            <div><dt>总成本</dt><dd>{formatCostUsd(focus.total_cost_usd)}</dd></div>
            <div><dt>成本/题</dt><dd>{formatCostUsd(focus.cost_per_task_usd)}</dd></div>
            <div><dt>P50 延迟</dt><dd>{focus.latency_p50_ms === null || focus.latency_p50_ms === undefined ? "—" : `${focus.latency_p50_ms} ms`}</dd></div>
            <div><dt>P95 延迟</dt><dd>{focus.latency_p95_ms === null || focus.latency_p95_ms === undefined ? "—" : `${focus.latency_p95_ms} ms`}</dd></div>
            <div><dt>延迟样本</dt><dd>{focus.latency_sample_count ?? "—"}</dd></div>
          </dl>
        </section>
        <section className="drawer-section">
          <p className="eyebrow">BENCHMARK DETAIL</p>
          <BenchmarkBreakdown result={focus} />
        </section>
        <section className="drawer-section drawer-source">
          <p className="eyebrow">RESULT SOURCE</p>
          <strong>{focus.source_run_name ?? "暂无正式结果"}</strong>
          {focus.source_profile_key && <code>{focus.source_profile_key}</code>}
          <span>{formatPublished(focus.source_published_at)}</span>
          {focus.source_run_id && <small>RUN {focus.source_run_id.slice(0, 12)}</small>}
        </section>
        <footer>
          {isOperator ? <><Link className="button is-ghost" to={evaluationLink(agent, focusMetric)}>重新评测</Link>{focus.source_run_id && <Link className="button" to={`/runs/${focus.source_run_id}`}>查看任务</Link>}</> : <>
            <Link className="button is-ghost" to={`/plaza/${agent.agent_id}`}>公开详情与趋势</Link>
            {workspaceEnabled && agent.agent_version_id && <Link className="button" to={`/workspace?agentVersion=${agent.agent_version_id}`}>在线运行</Link>}
            {workspaceEnabled && agent.agent_version_id && <Link className="button is-ghost" to={`/compare?agentVersion=${agent.agent_version_id}`}>加入实时对比</Link>}
          </>}
        </footer>
      </aside>
    </div>
  );
}

export function LeaderboardPage({ isOperator = true, workspaceEnabled = true }: { isOperator?: boolean; workspaceEnabled?: boolean }) {
  const [view, setView] = useState<BoardView>("overview");
  const [sortMetric, setSortMetric] = useState<GlobalLeaderboardMetric>("overall");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [search, setSearch] = useState("");
  const [provider, setProvider] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [selected, setSelected] = useState<string[]>([]);
  const [detailAgent, setDetailAgent] = useState<GlobalOverviewAgent | null>(null);
  const overviewQuery = useQuery({
    queryKey: ["leaderboard-global-overview"],
    queryFn: fetchGlobalLeaderboardOverview,
    retry: 1
  });

  if (overviewQuery.isPending) {
    return <main className="workspace"><LoadingState label="正在读取全球榜单" /></main>;
  }
  if (overviewQuery.error || !overviewQuery.data) {
    return <main className="workspace"><div className="table-state is-error">{overviewQuery.error?.message ?? "榜单读取失败"}</div></main>;
  }

  const overview = overviewQuery.data;
  const activeMetric = view === "overview" ? sortMetric : view;
  const protocol = overview.protocols.find((item) => item.metric === activeMetric);
  const providers = Array.from(new Set(overview.agents.map((agent) => agent.provider))).sort();
  const filtered = overview.agents.filter((agent) => {
    const matchesSearch = `${agent.agent_name} ${agent.provider}`.toLowerCase().includes(search.trim().toLowerCase());
    const matchesProvider = provider === "all" || agent.provider === provider;
    const relevant = view === "overview" ? leaderboardMetrics.map(({ key }) => agent.metrics[key]) : [agent.metrics[view]];
    const matchesStatus = statusFilter === "all"
      || (statusFilter === "ranked" && relevant.some((metric) => metric.rank !== null))
      || (statusFilter === "incomplete" && relevant.some((metric) => metric.rank === null))
      || (statusFilter === "needs_retest" && relevant.some((metric) => metric.status === "needs_retest"));
    return matchesSearch && matchesProvider && matchesStatus;
  });
  const ranked = filtered
    .filter((agent) => hasResult(agent, view === "overview" ? undefined : view))
    .sort((left, right) => {
      const leftRank = left.metrics[activeMetric].rank;
      const rightRank = right.metrics[activeMetric].rank;
      if (leftRank === null && rightRank === null) return 0;
      if (leftRank === null) return 1;
      if (rightRank === null) return -1;
      return sortDirection === "asc" ? leftRank - rightRank : rightRank - leftRank;
    });
  const unranked = filtered.filter((agent) => !hasResult(agent, view === "overview" ? undefined : view));
  const leaders = overview.agents
    .filter((agent) => agent.metrics[activeMetric].rank !== null)
    .sort((left, right) => (left.metrics[activeMetric].rank ?? 0) - (right.metrics[activeMetric].rank ?? 0))
    .slice(0, 3);

  function changeView(next: BoardView) {
    setView(next);
    if (next !== "overview") {
      setSortMetric(next);
      setSortDirection("asc");
    }
  }

  function toggleSort(metric: GlobalLeaderboardMetric) {
    if (sortMetric === metric) {
      setSortDirection((current) => (current === "asc" ? "desc" : "asc"));
      return;
    }
    setSortMetric(metric);
    setSortDirection("asc");
  }

  function toggleSelected(agentId: string) {
    setSelected((current) => current.includes(agentId)
      ? current.filter((id) => id !== agentId)
      : current.length < 5 ? [...current, agentId] : current);
  }

  return (
    <main className="workspace leaderboard-workspace stagger-in">
      <PageHeader
        title="Agent 排行榜"
        meta={
          <>
            <span className="leaderboard-protocol">
              <i aria-hidden="true" />
              当前协议
              <strong>{protocol?.published_at ? `${formatPublished(protocol.published_at, false)} 发布` : "等待正式成绩"}</strong>
            </span>
            <span>{formatPublished(overview.generated_at, false)} 更新 · {overview.eligible_agent_count} 个 Agent</span>
            <Link className="leaderboard-history-link" to="/leaderboard/history">查看历次榜单发布 →</Link>
            <details className="protocol-details">
              <summary>查看协议</summary>
              <div className="protocol-pop">
                <code title={protocol?.protocol_hash ?? undefined}>{protocol?.protocol_hash ?? "NO PROTOCOL"}</code>
                <p>{protocol?.question_count ?? 0} 题 · {protocol?.benchmark_count ?? 0} Benchmarks</p>
              </div>
            </details>
          </>
        }
      />

      <Tabs items={tabs} active={view} onChange={changeView} ariaLabel="排行榜视图" />

      <section className="leader-podium" aria-label={`${metricByKey[activeMetric].boardLabel}前三名`}>
        {[0, 1, 2].map((index) => {
          const agent = leaders[index];
          const result = agent?.metrics[activeMetric];
          return (
            <article key={agent?.agent_id ?? index} className={index === 0 ? "is-first" : ""}>
              <span className="podium-rank">{String(index + 1).padStart(2, "0")}</span>
              {agent ? (
                <>
                  <div className="podium-agent">
                    <small>{metricByKey[activeMetric].shortLabel}领先</small>
                    <strong>{agent.agent_name}</strong>
                    <p>{agent.provider}{agent.version === null ? "" : ` · V${agent.version}`}</p>
                  </div>
                  <b>{result?.score === null || result?.score === undefined ? "—" : formatScoreWithCi(result.score, result.ci_low, result.ci_high)}</b>
                </>
              ) : (
                <div className="podium-empty"><small>等待补位</small><strong>暂无 Agent</strong></div>
              )}
            </article>
          );
        })}
      </section>

      <section className="leaderboard-tools" aria-label="榜单筛选">
        <label><span>搜索</span><Input value={search} placeholder="Agent 或提供方" aria-label="搜索 Agent" onChange={(event) => setSearch(event.target.value)} /></label>
        <label><span>提供方</span><Select value={provider} aria-label="筛选提供方" onChange={(event) => setProvider(event.target.value)}><option value="all">全部提供方</option>{providers.map((item) => <option key={item}>{item}</option>)}</Select></label>
        <label><span>状态</span><Select value={statusFilter} aria-label="筛选评测状态" onChange={(event) => setStatusFilter(event.target.value)}><option value="all">全部状态</option><option value="ranked">已有成绩</option><option value="incomplete">成绩不完整</option><option value="needs_retest">待复测</option></Select></label>
        <p><strong>{ranked.length}</strong> 个当前结果<small>按{metricByKey[activeMetric].shortLabel}排名</small></p>
      </section>

      {ranked.length === 0 ? (
        <EmptyState
          className="leaderboard-empty"
          title="当前视图暂无可排名结果"
          description="完成对应的正式评测后，成绩会自动出现在这里。"
        />
      ) : view === "overview" ? (
        <section aria-label="Agent 能力总览">
          <Table className="leaderboard-table">
            <thead>
              <tr>
                <th className="check-cell">选择</th>
                <th data-numeric>名次</th>
                <th>Agent</th>
                {leaderboardMetrics.map((metric) => (
                  <th key={metric.key} data-numeric>
                    <button
                      type="button"
                      className="th-sort"
                      aria-label={`按${metric.shortLabel}排序`}
                      aria-pressed={sortMetric === metric.key}
                      onClick={() => toggleSort(metric.key)}
                    >
                      {metric.shortLabel}
                      {sortMetric === metric.key && <span className="sort-arrow" aria-hidden="true">{sortDirection === "asc" ? "↑" : "↓"}</span>}
                    </button>
                  </th>
                ))}
                <th data-numeric>pass@1</th>
                <th data-numeric>pass^k</th>
                <th data-numeric>CI 区间</th>
                <th data-numeric>成本/题</th>
                <th data-numeric>延迟 P50/P95</th>
                <th data-numeric>稳定性</th>
              </tr>
            </thead>
            <tbody>
              {ranked.map((agent) => {
                const quality = latestResult(agent, activeMetric);
                const rank = agent.metrics[activeMetric].rank;
                return (
                  <tr key={agent.agent_id} className={rank === 1 ? "is-leading" : ""} onClick={() => setDetailAgent(agent)}>
                    <CompareCheckbox agent={agent} selected={selected} onToggle={toggleSelected} />
                    <td className="rank-cell" data-numeric>{rank === null ? "—" : `#${String(rank).padStart(2, "0")}`}</td>
                    <AgentIdentityCell agent={agent} onOpenDetail={() => setDetailAgent(agent)} />
                    {leaderboardMetrics.map((metric) => <MetricTableCell key={metric.key} metric={metric.key} agent={agent} isOperator={isOperator} />)}
                    <td data-numeric>{formatPercent(quality?.pass_at_1)}</td>
                    <td data-numeric>{formatPercent(quality?.pass_pow_k)}</td>
                    <td data-numeric>{quality?.score === null || quality?.score === undefined ? "—" : formatScoreWithCi(quality.score, quality.ci_low, quality.ci_high)}</td>
                    <td data-numeric>{formatCostUsd(quality?.cost_per_task_usd ?? quality?.total_cost_usd)}</td>
                    <td data-numeric>{quality ? formatLatency(quality) : "—"}</td>
                    <td data-numeric>{formatPercent(quality?.success_rate, 1)}</td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </section>
      ) : (
        <section aria-label={metricByKey[view].boardLabel}>
          <Table className="leaderboard-table specialist-table">
            <thead>
              <tr>
                <th className="check-cell">选择</th>
                <th data-numeric>名次</th>
                <th>Agent</th>
                <th data-numeric>专项得分</th>
                <th>Benchmark 明细</th>
                <th data-numeric>成功率</th>
                <th data-numeric>异常率</th>
                <th>结果来源</th>
              </tr>
            </thead>
            <tbody>
              {ranked.map((agent) => {
                const result = agent.metrics[view];
                return (
                  <tr key={agent.agent_id} className={result.rank === 1 ? "is-leading" : ""} onClick={() => setDetailAgent(agent)}>
                    <CompareCheckbox agent={agent} selected={selected} onToggle={toggleSelected} />
                    <td className="rank-cell" data-numeric>{result.rank === null ? "—" : `#${String(result.rank).padStart(2, "0")}`}</td>
                    <AgentIdentityCell agent={agent} onOpenDetail={() => setDetailAgent(agent)} />
                    <td className={`metric-cell is-${result.status}`} data-numeric title={result.message ?? undefined}>
                      <MetricValue result={result} withCi />
                    </td>
                    <td><BenchmarkBreakdown result={result} /></td>
                    <td data-numeric>{formatPercent(result.success_rate)}</td>
                    <td data-numeric>{formatPercent(result.anomaly_rate)}</td>
                    <td className="source-cell">
                      <strong>{result.source_run_name ?? "—"}</strong>
                      {result.source_profile_key && <code>{result.source_profile_key}</code>}
                      <span>{formatPublished(result.source_published_at, false)}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </section>
      )}

      <details className="unranked-accordion">
        <summary><span>尚未取得{view === "overview" ? "任何" : metricByKey[view].shortLabel}正式成绩的 Agent</span><strong>{unranked.length}</strong><small>{isOperator ? "展开查看与发起评测" : "展开查看可用 Agent"}</small></summary>
        {unranked.length ? <div>{unranked.map((agent) => <article key={agent.agent_id}><span><Link to={`/plaza/${agent.agent_id}`}><strong>{agent.agent_name}</strong></Link><small>{agent.provider}{agent.version === null ? " · 尚无版本" : ` · V${agent.version}`}</small></span><b>{statusLabels[agent.metrics[activeMetric].status]}</b>{isOperator ? <Link to={evaluationLink(agent, activeMetric)}>开始评测 →</Link> : workspaceEnabled && agent.agent_version_id ? <Link to={`/workspace?agentVersion=${agent.agent_version_id}`}>在线运行 →</Link> : <Link to={`/plaza/${agent.agent_id}`}>公开详情 →</Link>}</article>)}</div> : <p>当前筛选下没有待评测 Agent。</p>}
        <footer>{isOperator ? <Link to="/runs">前往评测任务 →</Link> : <Link to="/plaza">前往 Agent 广场 →</Link>}</footer>
      </details>

      <div className={`compare-dock ${selected.length ? "is-visible" : ""}`}>
        <div>
          <span>COMPARE</span>
          <strong>已选择 {selected.length} 个 Agent</strong>
          <small>{overview.agents.filter((agent) => selected.includes(agent.agent_id)).map((agent) => agent.agent_name).join("、")}</small>
        </div>
        <div className="compare-dock-actions">
          <button type="button" className="button button--ghost is-clear" onClick={() => setSelected([])}>清空</button>
          {selected.length >= 2 ? (
            <>
              <Link className="button" to={`/leaderboard/compare?agents=${selected.join(",")}`}>正式成绩对比</Link>
              {workspaceEnabled && <Link className="button button--ghost" to={`/compare?agents=${selected.join(",")}`}>实时回答对比 →</Link>}
            </>
          ) : <button type="button" className="button" disabled>至少选择两个</button>}
        </div>
      </div>

      <footer className="leaderboard-footnote"><span>RANKING POLICY</span><p>各维度仅汇总兼容协议下的最新正式成绩。未评测能力显示为空，不按 0 分处理；成功率和 HTTP 调用次数仅用于稳定性观测。</p></footer>
      {detailAgent && <AgentDetail agent={detailAgent} focusMetric={activeMetric} isOperator={isOperator} workspaceEnabled={workspaceEnabled} onClose={() => setDetailAgent(null)} />}
    </main>
  );
}
