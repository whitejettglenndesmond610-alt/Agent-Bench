import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { Link } from "react-router-dom";

import {
  AgentEvaluationStatus,
  fetchAgentEvaluationStatuses,
  fetchDashboardSummary,
  fetchRuns,
  RunListItem,
  RunStatus
} from "./api";
import { sparklineOption } from "./chartOptions";
import {
  CountUp,
  EmptyState,
  LoadingState,
  PageHeader,
  ProgressBar,
  StatusPill,
  StatusTone
} from "./components";
import { EChart } from "./EChart";
import { abilityMetrics } from "./metrics";
import "./dashboard.css";

const runStatusLabels: Record<RunStatus, string> = {
  draft: "草稿",
  queued: "排队中",
  running: "运行中",
  paused: "已暂停",
  scoring: "评分中",
  completed: "已完成",
  published: "已发布",
  system_failed: "系统失败",
  canceled: "已取消"
};

const runStatusTones: Record<RunStatus, StatusTone> = {
  draft: "neutral",
  queued: "warning",
  running: "info",
  paused: "warning",
  scoring: "info",
  completed: "success",
  published: "success",
  system_failed: "danger",
  canceled: "danger"
};

const ACTIVE_RUN_STATUSES: RunStatus[] = ["queued", "running", "paused", "scoring"];

const evaluationKeys: Array<keyof Pick<
  AgentEvaluationStatus,
  "comprehensive" | "reasoning" | "retrieval" | "data_reasoning" | "instruction_following" | "long_context"
>> = [
  "comprehensive",
  "reasoning",
  "retrieval",
  "data_reasoning",
  "instruction_following",
  "long_context"
];

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

// Cumulative count of matching runs ordered by creation time — the trend of
// how the queue/backlog built up, derived only from the runs query.
function cumulativeRunCounts(runs: RunListItem[], predicate: (run: RunListItem) => boolean): number[] {
  return runs
    .filter(predicate)
    .sort((left, right) => Date.parse(left.created_at) - Date.parse(right.created_at))
    .map((_, index) => index + 1);
}

// Cumulative evaluated-metric count ordered by each metric's last update.
function coverageTrend(statuses: AgentEvaluationStatus[]): number[] {
  return statuses
    .flatMap((agent) =>
      evaluationKeys
        .filter((key) => agent[key].status === "evaluated")
        .map((key) => agent[key].updated_at)
    )
    .filter((value): value is string => Boolean(value))
    .sort()
    .map((_, index) => index + 1);
}

function KpiSpark({ values, ariaLabel }: { values: number[]; ariaLabel: string }) {
  const option = useMemo(
    () => (values.length >= 2 ? sparklineOption(values) : null),
    [values]
  );
  return (
    <div className="kpi-spark">
      {option && <EChart option={option} className="kpi-spark-chart" ariaLabel={ariaLabel} fallbackHeight={40} />}
    </div>
  );
}

export function DashboardPage() {
  const summaryQuery = useQuery({
    queryKey: ["dashboard-summary"],
    queryFn: fetchDashboardSummary
  });
  const runsQuery = useQuery({
    queryKey: ["runs"],
    queryFn: fetchRuns,
    refetchInterval: 10_000
  });
  const statusesQuery = useQuery({
    queryKey: ["agent-evaluation-statuses"],
    queryFn: fetchAgentEvaluationStatuses,
    refetchInterval: 10_000
  });

  const runs = useMemo(() => runsQuery.data ?? [], [runsQuery.data]);
  const statuses = useMemo(() => statusesQuery.data ?? [], [statusesQuery.data]);

  const activeRuns = useMemo(
    () => runs.filter((run) => ACTIVE_RUN_STATUSES.includes(run.status)),
    [runs]
  );
  const publishedRuns = useMemo(
    () => runs.filter((run) => run.status === "published"),
    [runs]
  );

  const activeRunTrend = useMemo(
    () => cumulativeRunCounts(runs, (run) => ACTIVE_RUN_STATUSES.includes(run.status)),
    [runs]
  );
  const publishedRunTrend = useMemo(
    () => cumulativeRunCounts(runs, (run) => run.status === "published"),
    [runs]
  );
  const evaluatedMetricTrend = useMemo(() => coverageTrend(statuses), [statuses]);
  const abilityDistribution = useMemo(
    () => abilityMetrics.map((metric) => summaryQuery.data?.active_profile.ability_counts[metric.key] ?? 0),
    [summaryQuery.data]
  );

  const evaluatedMetrics = statuses.reduce(
    (count, agent) => count + evaluationKeys.filter((key) => agent[key].status === "evaluated").length,
    0
  );
  const totalMetrics = statuses.length * evaluationKeys.length;
  const coverage = totalMetrics ? Math.round(evaluatedMetrics * 100 / totalMetrics) : 0;

  if (summaryQuery.isPending) {
    return <main className="workspace"><LoadingState label="正在读取评测总览" /></main>;
  }
  if (summaryQuery.error || !summaryQuery.data) {
    return <main className="workspace"><div className="table-state is-error">{summaryQuery.error?.message ?? "评测总览读取失败"}</div></main>;
  }

  const summary = summaryQuery.data;
  const profile = summary.active_profile;
  const readyProfiles = summary.ranking_profiles.filter((item) => item.ready).length;

  return (
    <main className="workspace dashboard-workspace">
      <PageHeader
        title="评测总览"
        meta={(
          <>
            <span>{profile.name}</span>
            <span>{profile.key} · {profile.question_count} 题 · {profile.dataset_version_count} 个数据集版本</span>
          </>
        )}
        actions={(
          <>
            <Link className="button" to="/runs?launch=1">发起评测</Link>
            <Link className="button is-ghost" to="/workspace">在线运行 Agent</Link>
          </>
        )}
      />

      <section className="dashboard-kpi-grid stagger-in" aria-label="评测关键指标">
        <article className={`kpi-card ${profile.ready ? "is-ready" : "is-blocked"}`}>
          <span>当前评测档案</span>
          <strong>{profile.ready ? "就绪" : "未就绪"}</strong>
          <small>{profile.name} · {profile.key}</small>
        </article>
        <article className="kpi-card">
          <span>题目规模</span>
          <strong><CountUp value={profile.question_count} /></strong>
          <small>{profile.dataset_version_count} 个数据集版本</small>
          <KpiSpark values={abilityDistribution} ariaLabel="各能力维度的题目数量分布" />
        </article>
        <article className="kpi-card">
          <span>执行中运行</span>
          <strong>{runsQuery.isPending ? "—" : <CountUp value={activeRuns.length} />}</strong>
          <small>排队、运行、暂停或评分</small>
          <KpiSpark values={activeRunTrend} ariaLabel="执行中运行随时间的累计数量" />
        </article>
        <article className="kpi-card">
          <span>已发布运行</span>
          <strong>{runsQuery.isPending ? "—" : <CountUp value={publishedRuns.length} />}</strong>
          <small>正式结果记录</small>
          <KpiSpark values={publishedRunTrend} ariaLabel="已发布运行随时间的累计数量" />
        </article>
        <article className="kpi-card">
          <span>能力评测覆盖</span>
          <strong>{statusesQuery.isPending ? "—" : <CountUp value={coverage} format={(current) => `${Math.round(current)}%`} />}</strong>
          <small>{evaluatedMetrics} / {totalMetrics || 0} 项有效</small>
          <KpiSpark values={evaluatedMetricTrend} ariaLabel="已评测能力项随时间的累计数量" />
        </article>
      </section>

      <section className="dashboard-profile-grid">
        <article className="dashboard-profile-card">
          <header>
            <div>
              <h2>评测档案</h2>
              <p>{profile.name} · {profile.key}</p>
            </div>
            <StatusPill tone={profile.ready ? "success" : "warning"}>{profile.ready ? "就绪" : "待处理"}</StatusPill>
          </header>
          <div className="dashboard-profile-volume">
            <strong><CountUp value={profile.question_count} /></strong><span>QUESTIONS</span>
          </div>
          <dl className="dashboard-language-counts">
            <div><dt>中文</dt><dd>{profile.language_counts.zh ?? 0}</dd></div>
            <div><dt>英文</dt><dd>{profile.language_counts.en ?? 0}</dd></div>
            <div><dt>能力维度</dt><dd>{abilityMetrics.length}</dd></div>
            <div><dt>可用排名档案</dt><dd>{readyProfiles} / {summary.ranking_profiles.length}</dd></div>
          </dl>
          <footer>裁判模型：{summary.judge_model ?? "尚未配置"}</footer>
        </article>

        <article className="dashboard-ability-card">
          <header>
            <div>
              <h2>能力题目与权重</h2>
              <p>{abilityMetrics.length} 个能力维度 · 按题目数占比</p>
            </div>
          </header>
          <div>
            {abilityMetrics.map((metric) => {
              const count = profile.ability_counts[metric.key] ?? 0;
              const width = profile.question_count ? count * 100 / profile.question_count : 0;
              return (
                <div className="dashboard-ability-row" key={metric.key}>
                  <span>{metric.shortLabel}</span>
                  {/* metric.color 是 metrics.ts 数据驱动的能力色，保留内联 style。 */}
                  <div aria-hidden="true"><i style={{ width: `${width}%`, background: metric.color }} /></div>
                  <strong>{count} 题</strong>
                  <small>{profile.ability_weights[metric.key] ?? 0}%</small>
                </div>
              );
            })}
          </div>
        </article>
      </section>

      <section className="dashboard-activity-grid">
        <article className="dashboard-recent-runs">
          <header>
            <div>
              <h2>最近评测运行</h2>
              <p>{runsQuery.isPending ? "正在同步" : `${runs.length} 条运行记录`}</p>
            </div>
            <Link to="/runs">查看全部</Link>
          </header>
          {runsQuery.error ? (
            <p className="dashboard-inline-error">运行记录读取失败：{runsQuery.error.message}</p>
          ) : runsQuery.isPending ? (
            <LoadingState label="正在读取最近运行" compact />
          ) : runs.length ? (
            <ol>
              {runs.slice(0, 5).map((run) => {
                const processed = run.completed + run.failed;
                const percent = run.total ? Math.round(processed * 100 / run.total) : 0;
                const active = ACTIVE_RUN_STATUSES.includes(run.status);
                return (
                  <li key={run.id}>
                    <Link to={`/runs/${run.id}`}><strong>{run.name}</strong><small>{formatDate(run.created_at)} · {run.agent_count} Agent</small></Link>
                    <ProgressBar value={percent} active={active} label={`${run.name} 完成进度`} />
                    <StatusPill tone={runStatusTones[run.status]}>{runStatusLabels[run.status]}</StatusPill>
                    <b>{percent}%</b>
                  </li>
                );
              })}
            </ol>
          ) : (
            <EmptyState
              className="dashboard-empty-copy"
              title="尚无评测运行"
              description="发起一次评测以建立第一条能力记录。"
            />
          )}
        </article>

        <article className="dashboard-agent-readiness">
          <header>
            <div>
              <h2>Agent 评测状态</h2>
              <p>{statusesQuery.isPending ? "正在同步" : `${statuses.length} 个 Agent · ${evaluationKeys.length} 项能力`}</p>
            </div>
            <Link to="/agents">Agent 中心</Link>
          </header>
          {statusesQuery.error ? (
            <p className="dashboard-inline-error">Agent 状态读取失败：{statusesQuery.error.message}</p>
          ) : statusesQuery.isPending ? (
            <LoadingState label="正在读取 Agent 状态" compact />
          ) : statuses.length ? (
            <ol>
              {statuses.slice(0, 5).map((agent) => {
                const evaluated = evaluationKeys.filter((key) => agent[key].status === "evaluated").length;
                const active = evaluationKeys.some((key) => ["queued", "running"].includes(agent[key].status));
                const tone: StatusTone = active ? "info" : evaluated === evaluationKeys.length ? "success" : "neutral";
                return (
                  <li key={agent.agent_id}>
                    <Link to={`/agents/${agent.agent_id}`}><strong>{agent.agent_name}</strong><small>{agent.provider} · V{agent.version}</small></Link>
                    <StatusPill tone={tone}>{active ? "评测中" : `${evaluated} / ${evaluationKeys.length} 已评测`}</StatusPill>
                  </li>
                );
              })}
            </ol>
          ) : (
            <EmptyState
              className="dashboard-empty-copy"
              title="尚无可评测 Agent"
              description="先在 Agent 中心接入外部 Agent API。"
            />
          )}
        </article>
      </section>

      <section className="dashboard-cta-strip" aria-label="快捷操作">
        <Link to="/agents"><span>01</span><strong>管理 Agent</strong><small>版本、预检与配置</small></Link>
        <Link to="/compare"><span>02</span><strong>并行在线对比</strong><small>同一问题，多 Agent 作答</small></Link>
        <Link to="/experiments"><span>03</span><strong>查看评测实验</strong><small>聚合进度与能力矩阵</small></Link>
        <Link to="/leaderboard"><span>04</span><strong>查看排行榜</strong><small>正式发布成绩</small></Link>
      </section>
    </main>
  );
}
