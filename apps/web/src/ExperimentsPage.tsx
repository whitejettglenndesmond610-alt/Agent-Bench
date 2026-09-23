import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import {
  EvaluationExperimentMember,
  EvaluationExperimentSummary,
  fetchEvaluationExperiment,
  fetchEvaluationExperiments,
  RunStatus
} from "./api";
import { LoadingState, PageHeader, Table } from "./components";
import { leaderboardMetrics } from "./metrics";
import "./experiments.css";

const terminalStatuses = new Set<RunStatus>([
  "completed",
  "published",
  "system_failed",
  "canceled"
]);

const statusLabels: Record<RunStatus, string> = {
  draft: "草稿",
  queued: "排队中",
  running: "运行中",
  paused: "已暂停",
  scoring: "评分中",
  completed: "已完成",
  published: "已发布",
  system_failed: "部分失败",
  canceled: "已取消"
};

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function profileLabel(experiment: Pick<EvaluationExperimentSummary, "run_scope" | "target_ability" | "profile_key">) {
  if (experiment.run_scope === "smoke") return "冒烟验证";
  if (experiment.run_scope === "specialist" && experiment.target_ability) {
    return `${leaderboardMetrics.find((metric) => metric.key === experiment.target_ability)?.shortLabel ?? experiment.target_ability}专项`;
  }
  return "综合评测";
}

function ExperimentList() {
  const experimentsQuery = useQuery({
    queryKey: ["evaluation-experiments"],
    queryFn: fetchEvaluationExperiments,
    refetchInterval: (query) => query.state.data?.some((item) => !terminalStatuses.has(item.status)) ? 5_000 : false
  });
  const experiments = experimentsQuery.data ?? [];
  const active = experiments.filter((item) => !terminalStatuses.has(item.status)).length;
  const complete = experiments.filter((item) => item.status === "completed" || item.status === "published").length;

  return (
    <main className="workspace experiments-workspace stagger-in">
      <PageHeader
        title="评测实验"
        meta={
          <>
            <span className="experiments-eyebrow">MULTI-AGENT EVALUATION</span>
            <span>将多个 Agent 的独立运行固化为一个可追踪实验，在相同协议下比较进度、成绩与运行质量。</span>
          </>
        }
        actions={<Link className="button" to="/runs?launch=1">+ 发起多 Agent 实验</Link>}
      />
      <section className="experiment-summary-strip">
        <div><span>实验总数</span><strong>{experiments.length}</strong></div>
        <div><span>进行中</span><strong>{active}</strong></div>
        <div><span>已完成</span><strong>{complete}</strong></div>
        <p>实验成员是可独立查看证据的单 Agent 评测运行。</p>
      </section>
      {experimentsQuery.isPending ? <LoadingState label="正在读取评测实验" /> : experimentsQuery.error ? <div className="table-state is-error">{experimentsQuery.error.message}</div> : experiments.length === 0 ? <section className="empty-state"><span>NO EXPERIMENTS</span><h2>尚无评测实验</h2><p>在发起评测时选择至少两个 Agent，系统会建立持久化实验。</p><Link className="button" to="/runs?launch=1">创建首个实验</Link></section> : (
        <div className="experiment-list-stack">
          {experiments.map((experiment) => <article key={experiment.id}>
            <header><div><span>EXP / {experiment.id.slice(0, 8).toUpperCase()}</span><h2><Link to={`/experiments/${experiment.id}`}>{experiment.name}</Link></h2><small>{profileLabel(experiment)} · {experiment.profile_key}</small></div><span className={`experiment-status is-${experiment.status}`}><i />{statusLabels[experiment.status]}</span></header>
            <div className="experiment-list-progress"><div><i style={{ width: `${experiment.progress_percent}%` }} /></div><strong>{experiment.progress_percent.toFixed(2)}%</strong></div>
            <dl><div><dt>Agent</dt><dd>{experiment.member_count}</dd></div><div><dt>处理</dt><dd>{experiment.processed} / {experiment.total}</dd></div><div><dt>成功</dt><dd>{experiment.completed}</dd></div><div><dt>失败</dt><dd>{experiment.failed}</dd></div><div><dt>创建人</dt><dd>{experiment.creator.display_name}</dd></div><div><dt>更新时间</dt><dd>{formatDate(experiment.updated_at)}</dd></div></dl>
            <footer><code title={experiment.protocol_hash}>{experiment.protocol_hash.slice(0, 16)}…</code><Link to={`/experiments/${experiment.id}`}>查看实验 →</Link></footer>
          </article>)}
        </div>
      )}
    </main>
  );
}

function metricScore(member: EvaluationExperimentMember, metric: string) {
  const value = member.metric_scores[metric];
  return value === undefined ? null : value;
}

function ExperimentDetail({ experimentId }: { experimentId: string }) {
  const experimentQuery = useQuery({
    queryKey: ["evaluation-experiment", experimentId],
    queryFn: () => fetchEvaluationExperiment(experimentId),
    refetchInterval: (query) => {
      const experiment = query.state.data;
      return experiment && !terminalStatuses.has(experiment.status)
        ? document.visibilityState === "visible" ? 3_000 : 15_000
        : false;
    }
  });

  if (experimentQuery.isPending) return <main className="workspace"><LoadingState label="正在读取评测实验" /></main>;
  if (experimentQuery.error || !experimentQuery.data) return <main className="workspace"><div className="table-state is-error">{experimentQuery.error?.message ?? "评测实验不存在"}</div></main>;

  const experiment = experimentQuery.data;
  const metrics = leaderboardMetrics.map((metric) => ({
    ...metric,
    comparable: experiment.comparison_metrics.find((item) => item.metric === metric.key)
  }));

  return (
    <main className="workspace experiment-detail-workspace">
      <Link className="back-link" to="/experiments">← 返回评测实验</Link>
      <PageHeader
        className="experiment-detail-header"
        title={experiment.name}
        meta={
          <>
            <span className="experiments-eyebrow">EXPERIMENT / {experiment.id.slice(0, 8).toUpperCase()}</span>
            <span>{profileLabel(experiment)} · {experiment.profile_key} · {experiment.member_count} 个 Agent</span>
          </>
        }
        actions={<span className={`experiment-status is-${experiment.status}`}><i />{statusLabels[experiment.status]}</span>}
      />

      <section className="experiment-overall-progress">
        <header><div><span>AGGREGATE PROGRESS</span><strong>{experiment.progress_percent.toFixed(2)}%</strong></div><p>{experiment.processed} / {experiment.total} 执行项已处理</p></header>
        <div><i style={{ width: `${experiment.progress_percent}%` }} /></div>
        <dl><div><dt>Agent</dt><dd>{experiment.member_count}</dd></div><div><dt>成功</dt><dd>{experiment.completed}</dd></div><div><dt>失败</dt><dd>{experiment.failed}</dd></div><div><dt>进行中成员</dt><dd>{experiment.run_counts.queued + experiment.run_counts.running + experiment.run_counts.scoring}</dd></div><div><dt>创建人</dt><dd>{experiment.creator.display_name}</dd></div></dl>
      </section>

      <section className="experiment-member-grid">
        {experiment.members.map((member) => (
          <article key={member.run_id}>
            <header><span>{String(member.position).padStart(2, "0")}</span><div><strong>{member.agent_name}</strong><small>{member.provider} · V{member.version}</small></div><b>{statusLabels[member.status]}</b></header>
            <div className="experiment-member-score"><strong>{metricScore(member, "overall")?.toFixed(2) ?? "—"}</strong><span>{member.metric_ranks.overall ? `综合 #${member.metric_ranks.overall}` : "综合成绩不可用"}</span></div>
            <div className="experiment-member-bar"><i style={{ width: `${member.progress_percent}%` }} /></div>
            <dl><div><dt>进度</dt><dd>{member.processed} / {member.total}</dd></div><div><dt>失败</dt><dd>{member.failed}</dd></div><div><dt>重试</dt><dd>{member.retried}</dd></div><div><dt>成功率</dt><dd>{member.result ? `${member.result.success_rate.toFixed(2)}%` : "—"}</dd></div><div><dt>P50 / P95</dt><dd>{member.result ? `${member.result.latency_p50_ms ?? "—"} / ${member.result.latency_p95_ms ?? "—"} ms` : "—"}</dd></div><div><dt>Token</dt><dd>{member.result?.total_tokens == null ? "—" : new Intl.NumberFormat("zh-CN").format(member.result.total_tokens)}</dd></div></dl>
            <footer><Link to={`/runs/${member.run_id}`}>子运行</Link><Link to={`/runs/${member.run_id}/evidence`}>运行证据</Link><Link to={`/agents/${member.agent_id}`}>Agent 详情</Link></footer>
          </article>
        ))}
      </section>

      <section className="experiment-score-section">
        <header><div><p className="eyebrow">COMPARABLE RESULTS</p><h2>成绩与排名矩阵</h2></div><small>“不可用”表示尚无同协议结果，绝不按 0 分处理</small></header>
        <Table className="experiment-score-table">
          <thead><tr><th>Agent</th>{metrics.map((metric) => <th key={metric.key}>{metric.shortLabel}<small>{metric.comparable ? `${metric.comparable.compared_count} 可比` : "不可比"}</small></th>)}</tr></thead>
          <tbody>{experiment.members.map((member) => <tr key={member.run_id}><td><strong>{member.agent_name}</strong><small>{member.provider} · V{member.version}</small></td>{metrics.map((metric) => {
            const score = metricScore(member, metric.key);
            const rank = member.metric_ranks[metric.key];
            return <td key={metric.key}>{score === null ? <span className="experiment-missing-score">不可用</span> : <div className="experiment-score-cell"><strong>{score.toFixed(2)}</strong><span>{rank ? `#${rank}` : "—"}</span><i style={{ width: `${Math.max(0, Math.min(100, score))}%`, background: metric.color }} /></div>}</td>;
          })}</tr>)}</tbody>
        </Table>
      </section>

      <footer className="experiment-protocol-note"><span>PROTOCOL</span><code>{experiment.protocol_hash}</code><p>矩阵只比较后端确认使用同一协议的正式结果。成员运行可以独立完成、失败或保留证据。</p></footer>
    </main>
  );
}

export function ExperimentsPage() {
  const { experimentId } = useParams();
  return experimentId ? <ExperimentDetail experimentId={experimentId} /> : <ExperimentList />;
}
