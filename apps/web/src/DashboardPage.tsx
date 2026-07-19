import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import {
  AgentEvaluationStatus,
  fetchAgentEvaluationStatuses,
  fetchDashboardSummary,
  fetchRuns,
  RunStatus
} from "./api";
import { LoadingState } from "./components";
import { abilityMetrics } from "./metrics";

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

  if (summaryQuery.isPending) {
    return <main className="workspace"><LoadingState label="正在读取评测总览" /></main>;
  }
  if (summaryQuery.error || !summaryQuery.data) {
    return <main className="workspace"><div className="table-state is-error">{summaryQuery.error?.message ?? "评测总览读取失败"}</div></main>;
  }

  const summary = summaryQuery.data;
  const profile = summary.active_profile;
  const runs = runsQuery.data ?? [];
  const statuses = statusesQuery.data ?? [];
  const activeRuns = runs.filter((run) => ["queued", "running", "paused", "scoring"].includes(run.status));
  const publishedRuns = runs.filter((run) => run.status === "published");
  const evaluatedMetrics = statuses.reduce(
    (count, agent) => count + evaluationKeys.filter((key) => agent[key].status === "evaluated").length,
    0
  );
  const totalMetrics = statuses.length * evaluationKeys.length;
  const coverage = totalMetrics ? Math.round(evaluatedMetrics * 100 / totalMetrics) : 0;
  const readyProfiles = summary.ranking_profiles.filter((item) => item.ready).length;

  return (
    <main className="workspace dashboard-workspace">
      <header className="section-header dashboard-product-header">
        <div>
          <p className="eyebrow">AGENT EVALUATION OVERVIEW</p>
          <h1>让每个 Agent 的能力<br />都有证据可查。</h1>
          <p>从在线试跑到正式实验，持续追踪评测覆盖、运行进度与可发布结果。</p>
        </div>
        <div className="dashboard-primary-actions">
          <Link className="button" to="/runs?launch=1">发起评测</Link>
          <Link className="button is-ghost" to="/workspace">在线运行 Agent</Link>
        </div>
      </header>

      <section className="dashboard-kpi-grid" aria-label="评测关键指标">
        <article className={profile.ready ? "is-ready" : "is-blocked"}>
          <span>当前评测档案</span>
          <strong>{profile.ready ? "就绪" : "未就绪"}</strong>
          <small>{profile.name} · {profile.key}</small>
        </article>
        <article><span>题目规模</span><strong>{profile.question_count}</strong><small>{profile.dataset_version_count} 个数据集版本</small></article>
        <article><span>执行中运行</span><strong>{runsQuery.isPending ? "—" : activeRuns.length}</strong><small>排队、运行、暂停或评分</small></article>
        <article><span>已发布运行</span><strong>{runsQuery.isPending ? "—" : publishedRuns.length}</strong><small>正式结果记录</small></article>
        <article><span>能力评测覆盖</span><strong>{statusesQuery.isPending ? "—" : `${coverage}%`}</strong><small>{evaluatedMetrics} / {totalMetrics || 0} 项有效</small></article>
      </section>

      <section className="dashboard-profile-grid">
        <article className="dashboard-profile-card">
          <header>
            <div><p className="eyebrow">ACTIVE PROFILE</p><h2>{profile.name}</h2></div>
            <span className={profile.ready ? "is-ready" : "is-blocked"}>{profile.ready ? "READY" : "ACTION REQUIRED"}</span>
          </header>
          <div className="dashboard-profile-volume">
            <strong>{profile.question_count}</strong><span>QUESTIONS</span>
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
          <header><p className="eyebrow">ABILITY COVERAGE</p><h2>能力题目与权重</h2></header>
          <div>
            {abilityMetrics.map((metric) => {
              const count = profile.ability_counts[metric.key] ?? 0;
              const width = profile.question_count ? count * 100 / profile.question_count : 0;
              return (
                <div className="dashboard-ability-row" key={metric.key}>
                  <span>{metric.shortLabel}</span>
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
          <header><div><p className="eyebrow">RECENT ACTIVITY</p><h2>最近评测运行</h2></div><Link to="/runs">查看全部</Link></header>
          {runsQuery.error ? (
            <p className="dashboard-inline-error">运行记录读取失败：{runsQuery.error.message}</p>
          ) : runsQuery.isPending ? (
            <LoadingState label="正在读取最近运行" compact />
          ) : runs.length ? (
            <ol>
              {runs.slice(0, 5).map((run) => {
                const processed = run.completed + run.failed;
                const percent = run.total ? Math.round(processed * 100 / run.total) : 0;
                return (
                  <li key={run.id}>
                    <Link to={`/runs/${run.id}`}><strong>{run.name}</strong><small>{formatDate(run.created_at)} · {run.agent_count} Agent</small></Link>
                    <span className={`run-status is-${run.status}`}><i />{runStatusLabels[run.status]}</span>
                    <b>{percent}%</b>
                  </li>
                );
              })}
            </ol>
          ) : <p className="dashboard-empty-copy">尚无评测运行。发起一次评测以建立第一条能力记录。</p>}
        </article>

        <article className="dashboard-agent-readiness">
          <header><div><p className="eyebrow">AGENT COVERAGE</p><h2>Agent 评测状态</h2></div><Link to="/agents">Agent 中心</Link></header>
          {statusesQuery.error ? (
            <p className="dashboard-inline-error">Agent 状态读取失败：{statusesQuery.error.message}</p>
          ) : statusesQuery.isPending ? (
            <LoadingState label="正在读取 Agent 状态" compact />
          ) : statuses.length ? (
            <ol>
              {statuses.slice(0, 5).map((agent) => {
                const evaluated = evaluationKeys.filter((key) => agent[key].status === "evaluated").length;
                const active = evaluationKeys.some((key) => ["queued", "running"].includes(agent[key].status));
                return (
                  <li key={agent.agent_id}>
                    <Link to={`/agents/${agent.agent_id}`}><strong>{agent.agent_name}</strong><small>{agent.provider} · V{agent.version}</small></Link>
                    <span className={active ? "is-active" : evaluated === evaluationKeys.length ? "is-ready" : ""}>{active ? "评测中" : `${evaluated} / 6 已评测`}</span>
                  </li>
                );
              })}
            </ol>
          ) : <p className="dashboard-empty-copy">尚无可评测 Agent。先在 Agent 中心接入外部 Agent API。</p>}
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
