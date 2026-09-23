import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import {
  AgentEvaluationMetricStatus,
  AgentVersion,
  fetchAgent,
  fetchAgentEvaluationStatuses,
  fetchGlobalLeaderboardOverview,
  GlobalLeaderboardMetric
} from "./api";
import { LoadingState, PageHeader, StatusPill } from "./components";
import { leaderboardMetrics } from "./metrics";

import "./agent-detail.css";

const evaluationStatusLabels: Record<AgentEvaluationMetricStatus, string> = {
  not_evaluated: "未评测",
  queued: "排队中",
  running: "评测中",
  evaluated: "已评测",
  needs_retest: "待复测",
  failed: "评测失败",
  invalid: "配置无效"
};

const lifecycleLabels: Record<AgentVersion["lifecycle_status"], string> = {
  draft: "草稿",
  verified: "候选",
  active: "当前版本",
  retired: "历史版本"
};

const preflightLabels: Record<AgentVersion["preflight_status"], string> = {
  not_run: "未预检",
  running: "预检中",
  passed: "预检通过",
  failed: "预检失败"
};

function formatDate(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

export function AgentDetailPage() {
  const { agentId = "" } = useParams();
  const agentQuery = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => fetchAgent(agentId),
    enabled: Boolean(agentId)
  });
  const statusesQuery = useQuery({
    queryKey: ["agent-evaluation-statuses"],
    queryFn: fetchAgentEvaluationStatuses
  });
  const overviewQuery = useQuery({
    queryKey: ["leaderboard-global-overview"],
    queryFn: fetchGlobalLeaderboardOverview,
    retry: 1
  });

  if (agentQuery.isPending) {
    return <main className="workspace"><LoadingState label="正在读取 Agent 详情" /></main>;
  }
  if (agentQuery.error || !agentQuery.data) {
    return <main className="workspace"><div className="table-state is-error">{agentQuery.error?.message ?? "Agent 不存在"}</div></main>;
  }

  const agent = agentQuery.data;
  const versions = [...agent.versions].sort((left, right) => right.version - left.version);
  const activeVersion = versions.find((version) => version.lifecycle_status === "active");
  const candidateVersion = versions.find((version) => ["draft", "verified"].includes(version.lifecycle_status));
  const evaluation = statusesQuery.data?.find((item) => item.agent_id === agent.id);
  const globalAgent = overviewQuery.data?.agents.find((item) => item.agent_id === agent.id);

  return (
    <main className="workspace agent-detail-workspace">
      <Link className="back-link" to="/agents">← 返回 Agent 中心</Link>
      <PageHeader
        title={agent.name}
        meta={
          <>
            <span className="agent-avatar">{agent.logo_url ? <img src={agent.logo_url} alt="" /> : agent.name.slice(0, 2).toUpperCase()}</span>
            <span className="agent-meta-copy">{agent.provider} · {agent.description || "暂无 Agent 说明"}</span>
            <StatusPill tone={agent.status === "enabled" ? "success" : "neutral"}>{agent.status === "enabled" ? "已启用" : "已停用"}</StatusPill>
            <small>创建于 {formatDate(agent.created_at)}</small>
          </>
        }
        actions={activeVersion ? (
          <>
            <Link className="button" to={`/workspace?agentVersion=${encodeURIComponent(activeVersion.id)}`}>在线运行</Link>
            <Link className="button is-ghost" to={`/runs?launch=1&agentVersion=${encodeURIComponent(activeVersion.id)}`}>发起评测</Link>
            <Link className="button is-ghost" to={`/compare?agentVersion=${encodeURIComponent(activeVersion.id)}`}>并行对比</Link>
          </>
        ) : <span className="agent-action-unavailable">设置当前版本后可在线运行和评测</span>}
      />

      <section className="agent-version-overview">
        <article className="is-active">
          <span>CURRENT VERSION</span>
          <strong>{activeVersion ? `V${activeVersion.version}` : "未设置"}</strong>
          <small>{activeVersion ? `${preflightLabels[activeVersion.preflight_status]} · ${formatDate(activeVersion.created_at)}` : "需要将预检通过的候选版本设为当前版本"}</small>
        </article>
        <article>
          <span>CANDIDATE VERSION</span>
          <strong>{candidateVersion ? `V${candidateVersion.version}` : "无"}</strong>
          <small>{candidateVersion ? `${lifecycleLabels[candidateVersion.lifecycle_status]} · ${preflightLabels[candidateVersion.preflight_status]}` : "当前没有待处理候选"}</small>
        </article>
        <article>
          <span>VERSION HISTORY</span>
          <strong>{versions.length}</strong>
          <small>不可变配置版本</small>
        </article>
      </section>

      <section className="agent-evaluation-board">
        <header><div><p className="eyebrow">SIX-METRIC EVALUATION</p><h2>评测状态与正式成绩</h2></div><small>缺失结果不按 0 分处理</small></header>
        {statusesQuery.error ? <div className="dashboard-inline-error">评测状态读取失败：{statusesQuery.error.message}</div> : (
          <div className="agent-metric-grid">
            {leaderboardMetrics.map((metric) => {
              const statusKey = metric.key === "overall" ? "comprehensive" : metric.key;
              const state = evaluation?.[statusKey];
              const globalMetric = globalAgent?.metrics[metric.key as GlobalLeaderboardMetric];
              const score = state?.score ?? globalMetric?.score ?? null;
              const status = state?.status ?? globalMetric?.status ?? "not_evaluated";
              return (
                <article className={`is-${status}`} key={metric.key}>
                  <header><span style={{ background: metric.color }} /><strong>{metric.shortLabel}</strong><small>{evaluationStatusLabels[status]}</small></header>
                  <b>{score === null ? "—" : score.toFixed(2)}</b>
                  <footer><span>{globalMetric?.rank == null ? "暂无排名" : `正式排名 #${globalMetric.rank}`}</span>{state?.run_id && <Link to={`/runs/${state.run_id}`}>来源运行</Link>}</footer>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className="agent-detail-lower-grid">
        <article className="agent-config-summary">
          <header><p className="eyebrow">ACTIVE CONFIGURATION</p><h2>当前配置摘要</h2></header>
          {activeVersion ? (
            <dl>
              <div><dt>Agent API</dt><dd className="agent-endpoint">{activeVersion.endpoint_url}</dd></div>
              <div><dt>鉴权</dt><dd>{activeVersion.auth_type === "bearer" ? activeVersion.token_configured ? "Bearer · 凭据已安全配置" : "Bearer · 凭据缺失" : "无鉴权"}</dd></div>
              <div><dt>答案映射</dt><dd><code>{activeVersion.answer_jsonpath}</code></dd></div>
              <div><dt>引用映射</dt><dd>{activeVersion.citations_jsonpath ? <code>{activeVersion.citations_jsonpath}</code> : "未配置"}</dd></div>
              <div><dt>超时 / 并发</dt><dd>{activeVersion.timeout_seconds} 秒 / {activeVersion.concurrency} 路</dd></div>
              <div><dt>Prompt 上限</dt><dd>{activeVersion.max_prompt_characters ? `${activeVersion.max_prompt_characters} 字符` : "不限制"}</dd></div>
            </dl>
          ) : <p className="dashboard-empty-copy">没有可用于正式运行的当前版本。</p>}
          <footer>鉴权 Token 不会在详情页或配置摘要中显示。</footer>
        </article>

        <article className="agent-version-timeline">
          <header><p className="eyebrow">VERSION & PREFLIGHT</p><h2>版本与预检时间线</h2></header>
          {versions.length ? (
            <ol>
              {versions.map((version) => (
                <li className={`is-${version.lifecycle_status}`} key={version.id}>
                  <i />
                  <div><strong>V{version.version} · {lifecycleLabels[version.lifecycle_status]}</strong><span>{preflightLabels[version.preflight_status]} · {formatDate(version.preflight_at ?? version.created_at)}</span></div>
                  <b>{version.preflight_report.filter((check) => check.status === "passed").length}/{version.preflight_report.length || "—"}</b>
                  {version.preflight_report.length > 0 && <details><summary>查看预检项目</summary>{version.preflight_report.map((check) => <p key={check.key}><span>{check.label}</span><strong>{check.status === "passed" ? "通过" : check.status === "failed" ? "失败" : "跳过"}</strong>{check.error && <small>{check.error}</small>}</p>)}</details>}
                </li>
              ))}
            </ol>
          ) : <p className="dashboard-empty-copy">尚未创建 Agent 配置版本。</p>}
        </article>
      </section>
    </main>
  );
}
