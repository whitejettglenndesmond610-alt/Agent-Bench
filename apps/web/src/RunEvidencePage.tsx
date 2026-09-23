import { useQuery } from "@tanstack/react-query";
import { FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import {
  Ability,
  ExecutionStatus,
  fetchRunEvidence,
  fetchRunEvidenceDetail,
  RunEvidenceArtifactMetadata,
  RunEvidenceCitation
} from "./api";
import { Button, Input, LoadingState, Select, Table } from "./components";
import { TrajectoryView } from "./TrajectoryView";
import { metricByKey, metricDefinitionFor } from "./metrics";

const PAGE_SIZE = 25;

const evidenceStatusLabels: Record<ExecutionStatus, string> = {
  pending: "等待执行",
  in_progress: "执行中",
  answered: "等待评分",
  failed: "执行失败",
  scored: "已评分",
  canceled: "已取消"
};

const candidateStatusLabels = {
  available: "已保留",
  not_recorded: "未记录",
  expired: "已过期",
  unavailable: "不可用"
} as const;

function formatDate(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(new Date(value));
}

function safeHttpHref(value: string) {
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:")
      && !url.username
      && !url.password
      ? value
      : undefined;
  } catch {
    return undefined;
  }
}

function ArtifactMetadata({ artifact, label }: { artifact: RunEvidenceArtifactMetadata | null; label: string }) {
  if (!artifact) return null;
  return (
    <article className="evidence-artifact">
      <header><span>{label}</span><strong>{artifact.retained ? "保留中" : "按期清理"}</strong></header>
      <dl>
        <div><dt>类型</dt><dd>{artifact.kind}</dd></div>
        <div><dt>大小</dt><dd>{new Intl.NumberFormat("zh-CN").format(artifact.size_bytes)} bytes</dd></div>
        <div><dt>到期时间</dt><dd>{formatDate(artifact.expires_at)}</dd></div>
        <div><dt>SHA-256</dt><dd><code>{artifact.sha256}</code></dd></div>
      </dl>
    </article>
  );
}

function CitationCard({ citation }: { citation: RunEvidenceCitation }) {
  const destination = citation.final_url ?? citation.original_url;
  const href = safeHttpHref(destination);
  return (
    <article className={`evidence-citation is-${citation.fetch_status}`}>
      <header><span>引用 {citation.position + 1}</span><strong>{citation.fetch_status}</strong></header>
      {href ? <a href={href} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">{destination}</a> : <p>{destination}</p>}
      <dl>
        <div><dt>HTTP</dt><dd>{citation.http_status ?? "—"}</dd></div>
        <div><dt>真实性</dt><dd>{citation.authenticity_score == null ? "—" : citation.authenticity_score.toFixed(2)}</dd></div>
        <div><dt>支持度</dt><dd>{citation.support_score == null ? "—" : citation.support_score.toFixed(2)}</dd></div>
      </dl>
      {citation.judge_reason && <blockquote>{citation.judge_reason}</blockquote>}
      <ArtifactMetadata artifact={citation.content_artifact} label="引用内容留存" />
      <ArtifactMetadata artifact={citation.judge_artifact} label="引用评分留存" />
    </article>
  );
}

function EvidenceDetail({ runId, executionItemId }: { runId: string; executionItemId: string }) {
  const detailQuery = useQuery({
    queryKey: ["run-evidence-detail", runId, executionItemId],
    queryFn: () => fetchRunEvidenceDetail(runId, executionItemId)
  });

  if (detailQuery.isPending) {
    return <main className="workspace"><LoadingState label="正在读取题目证据" /></main>;
  }
  if (detailQuery.error || !detailQuery.data) {
    return <main className="workspace"><div className="table-state is-error">{detailQuery.error?.message ?? "题目证据不存在"}</div></main>;
  }

  const item = detailQuery.data;
  return (
    <main className="workspace run-evidence-workspace">
      <Link className="back-link" to={`/runs/${runId}/evidence`}>← 返回运行证据</Link>
      <header className="evidence-detail-header">
        <div><p className="eyebrow">EXECUTION EVIDENCE / {item.execution_item_id.slice(0, 8).toUpperCase()}</p><h1>{item.question_external_id}</h1><p>{item.benchmark_name} · {metricDefinitionFor(item.ability)?.label ?? item.ability} · {item.language.toUpperCase()}</p></div>
        <span className={`evidence-status is-${item.status}`}><i />{evidenceStatusLabels[item.status]}</span>
      </header>

      <section className="evidence-detail-meta">
        <div><span>Agent</span><strong>{item.agent_name}</strong><small>{item.provider} · V{item.version}</small></div>
        <div><span>最终分</span><strong>{item.score ? item.score.final_score.toFixed(2) : "—"}</strong><small>{item.score ? `答案 ${item.score.answer_score.toFixed(2)} · 引用 ${item.score.citation_score?.toFixed(2) ?? "—"}` : "尚无评分"}</small></div>
        <div><span>尝试 / 延迟</span><strong>{item.attempt_count}</strong><small>{item.latency_ms == null ? "延迟不可用" : `${item.latency_ms} ms`}</small></div>
        <div><span>回答留存</span><strong>{candidateStatusLabels[item.candidate_answer_status]}</strong><small>{item.response_artifact?.retained ? "Artifact 已标记保留" : "遵循 Artifact 到期策略"}</small></div>
      </section>

      <section className="evidence-content-grid">
        <article className="evidence-question-panel">
          <header><span>QUESTION</span><strong>评测问题</strong></header>
          <p>{item.prompt ?? "held-out 私有题，原文已遮蔽"}</p>
          {item.choices && <ol className="evidence-choices">{item.choices.map((choice) => <li key={choice.label}><b>{choice.label}</b><span>{choice.text}</span></li>)}</ol>}
          <footer><span>{item.question_type}</span><span>{item.requires_citations ? "要求引用" : "无需引用"}</span>{item.domain && <span>{item.domain}</span>}{item.difficulty && <span>{item.difficulty}</span>}</footer>
        </article>
        <article className="evidence-answer-panel is-reference">
          <header><span>REFERENCE</span><strong>参考答案</strong></header>
          <p>{item.reference_answer ?? (item.held_out ? "held-out 私有题，参考答案已遮蔽" : "未提供参考答案")}</p>
        </article>
        <article className="evidence-answer-panel is-candidate">
          <header><span>CANDIDATE</span><strong>Agent 回答</strong><small>{candidateStatusLabels[item.candidate_answer_status]}</small></header>
          {item.candidate_answer ? (
            <p>{item.candidate_answer}</p>
          ) : item.answer_preview ? (
            <><p>{item.answer_preview}</p><small className="evidence-preview-note">完整回答{candidateStatusLabels[item.candidate_answer_status]}，当前显示数据库保留的截断预览。</small></>
          ) : (
            <div className="evidence-unavailable">回答内容{candidateStatusLabels[item.candidate_answer_status]}，不以空文本代替。</div>
          )}
        </article>
      </section>

      {item.trajectory && <TrajectoryView trajectory={item.trajectory} />}

      <section className="evidence-judge-panel">
        <header><div><p className="eyebrow">JUDGE EVIDENCE</p><h2>评分与裁判理由</h2></div>{item.score && <strong>{item.score.final_score.toFixed(2)}</strong>}</header>
        {item.score ? (
          <>
            <dl><div><dt>答案分</dt><dd>{item.score.answer_score.toFixed(2)}</dd></div><div><dt>引用分</dt><dd>{item.score.citation_score?.toFixed(2) ?? "—"}</dd></div><div><dt>最终分</dt><dd>{item.score.final_score.toFixed(2)}</dd></div></dl>
            <blockquote>{item.score.judge_reason || "裁判未提供文字理由。"}</blockquote>
            <ArtifactMetadata artifact={item.score.judge_artifact} label="裁判响应留存" />
          </>
        ) : <p className="dashboard-empty-copy">当前执行项尚未完成评分，分数与理由不可用。</p>}
      </section>

      <section className="evidence-attempt-panel">
        <header><p className="eyebrow">ATTEMPT TIMELINE</p><h2>请求尝试</h2></header>
        {item.attempts.length ? <ol>{item.attempts.map((attempt) => <li className={`is-${attempt.status}`} key={attempt.attempt_id}><i /><div><strong>尝试 {attempt.attempt_no} · {attempt.status}</strong><span>{formatDate(attempt.started_at)} → {formatDate(attempt.finished_at)}</span>{attempt.error_type && <small>{attempt.error_type}</small>}</div><dl><div><dt>HTTP</dt><dd>{attempt.status_code ?? "—"}</dd></div><div><dt>延迟</dt><dd>{attempt.latency_ms == null ? "—" : `${attempt.latency_ms} ms`}</dd></div><div><dt>Token</dt><dd>{attempt.total_tokens ?? "—"}</dd></div></dl></li>)}</ol> : <p className="dashboard-empty-copy">尚无请求尝试记录。</p>}
      </section>

      {item.citations.length > 0 && <section className="evidence-citation-panel"><header><p className="eyebrow">CITATION EVIDENCE</p><h2>引用验证</h2></header><div>{item.citations.map((citation) => <CitationCard citation={citation} key={citation.citation_id} />)}</div></section>}
      <section className="evidence-retention-panel"><header><p className="eyebrow">SAFE RETENTION METADATA</p><h2>Artifact 留存</h2></header><p>此处仅显示不可执行的留存元数据，不直接加载或渲染 Artifact 内容。</p><ArtifactMetadata artifact={item.response_artifact} label="Agent 响应留存" /></section>
    </main>
  );
}

function EvidenceList({ runId }: { runId: string }) {
  const [parameters, setParameters] = useSearchParams();
  const offset = Math.max(0, Number(parameters.get("offset") ?? 0) || 0);
  const status = parameters.get("status") as ExecutionStatus | null;
  const ability = parameters.get("ability") as Ability | null;
  const language = parameters.get("language") as "zh" | "en" | null;
  const runAgentId = parameters.get("run_agent_id") ?? undefined;
  const errorType = parameters.get("error_type") ?? undefined;
  const minScore = parameters.has("min_score") ? Number(parameters.get("min_score")) : undefined;
  const maxScore = parameters.has("max_score") ? Number(parameters.get("max_score")) : undefined;
  const invalidRange = minScore !== undefined && maxScore !== undefined && minScore > maxScore;
  const evidenceQuery = useQuery({
    queryKey: ["run-evidence", runId, runAgentId, status, ability, language, errorType, minScore, maxScore, offset],
    queryFn: () => fetchRunEvidence(runId, {
      ...(runAgentId ? { run_agent_id: runAgentId } : {}),
      ...(status ? { status } : {}),
      ...(ability ? { ability } : {}),
      ...(language ? { language } : {}),
      ...(errorType ? { error_type: errorType } : {}),
      ...(minScore !== undefined ? { min_score: minScore } : {}),
      ...(maxScore !== undefined ? { max_score: maxScore } : {}),
      limit: PAGE_SIZE,
      offset
    }),
    enabled: !invalidRange
  });

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const next = new URLSearchParams();
    for (const key of ["run_agent_id", "status", "ability", "language", "error_type", "min_score", "max_score"]) {
      const value = String(form.get(key) ?? "").trim();
      if (value) next.set(key, value);
    }
    setParameters(next);
  }

  const evidence = evidenceQuery.data;
  const pageStart = evidence && evidence.total ? evidence.offset + 1 : 0;
  const pageEnd = evidence ? Math.min(evidence.offset + evidence.items.length, evidence.total) : 0;

  return (
    <main className="workspace run-evidence-workspace">
      <Link className="back-link" to={`/runs/${runId}`}>← 返回运行详情</Link>
      <header className="section-header evidence-list-header"><div><p className="eyebrow">RUN EVIDENCE</p><h1>运行证据</h1><p>逐题检查 Agent 回答、评分、失败原因、引用验证与安全留存元数据。</p></div><code>RUN {runId.slice(0, 12).toUpperCase()}</code></header>

      <form className="evidence-filter-bar" key={parameters.toString()} onSubmit={applyFilters}>
        <label><span>状态</span><Select name="status" defaultValue={status ?? ""}><option value="">全部状态</option>{Object.entries(evidenceStatusLabels).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</Select></label>
        <label><span>能力</span><Select name="ability" defaultValue={ability ?? ""}><option value="">全部能力</option>{Object.entries(metricByKey).filter(([key]) => key !== "overall").map(([key, metric]) => <option value={key} key={key}>{metric.shortLabel}</option>)}</Select></label>
        <label><span>语言</span><Select name="language" defaultValue={language ?? ""}><option value="">全部语言</option><option value="zh">中文</option><option value="en">英文</option></Select></label>
        <label><span>错误类型</span><Input name="error_type" defaultValue={errorType ?? ""} placeholder="例如 agent_timeout" /></label>
        <label><span>最低分</span><Input name="min_score" type="number" min="0" max="100" step="0.01" defaultValue={minScore ?? ""} /></label>
        <label><span>最高分</span><Input name="max_score" type="number" min="0" max="100" step="0.01" defaultValue={maxScore ?? ""} /></label>
        {runAgentId && <input type="hidden" name="run_agent_id" value={runAgentId} />}
        <Button type="submit">应用筛选</Button>
        <Link className="button is-ghost" to={`/runs/${runId}/evidence`}>清除</Link>
      </form>
      {runAgentId && <div className="evidence-active-filter">已限定单个 Agent 运行记录 <code>{runAgentId}</code></div>}
      {invalidRange && <div className="result-banner is-error" role="alert">最低分不能高于最高分。</div>}

      {evidenceQuery.isPending ? <LoadingState label="正在读取运行证据" /> : evidenceQuery.error ? <div className="table-state is-error">{evidenceQuery.error.message}</div> : !evidence?.items.length ? <section className="empty-state"><span>NO EVIDENCE</span><h2>没有匹配的证据项</h2><p>调整筛选条件，或等待运行产生执行记录。</p></section> : (
        <>
          <div className="evidence-list-summary"><span>共 {evidence.total} 项</span><strong>{pageStart}–{pageEnd}</strong></div>
          <Table className="evidence-table">
            <thead><tr><th>题目 / Agent</th><th>能力 / 语言</th><th>状态</th><th>分数</th><th>尝试 / 延迟</th><th><span className="sr-only">查看</span></th></tr></thead>
            <tbody>{evidence.items.map((item) => <tr key={item.execution_item_id}><td><strong>{item.question_external_id}</strong><small>{item.agent_name} · {item.provider} V{item.version}</small>{item.answer_preview && <p>{item.answer_preview}</p>}</td><td><span>{metricDefinitionFor(item.ability)?.shortLabel ?? item.ability}</span><small>{item.language.toUpperCase()} · {item.benchmark_name}</small></td><td><span className={`evidence-status is-${item.status}`}><i />{evidenceStatusLabels[item.status]}</span>{item.error_type && <small className="evidence-error-type">{item.error_type}</small>}</td><td className="evidence-score">{item.score ? item.score.final_score.toFixed(2) : "—"}</td><td><span>{item.attempt_count} 次</span><small>{item.latency_ms == null ? "延迟 —" : `${item.latency_ms} ms`}</small></td><td><Link className="row-link" to={`/runs/${runId}/evidence/${item.execution_item_id}`}>查看证据 →</Link></td></tr>)}</tbody>
          </Table>
          <nav className="evidence-pagination" aria-label="证据分页">
            <button type="button" disabled={offset === 0} onClick={() => { const next = new URLSearchParams(parameters); next.set("offset", String(Math.max(0, offset - PAGE_SIZE))); setParameters(next); }}>上一页</button>
            <span>{pageStart}–{pageEnd} / {evidence.total}</span>
            <button type="button" disabled={offset + PAGE_SIZE >= evidence.total} onClick={() => { const next = new URLSearchParams(parameters); next.set("offset", String(offset + PAGE_SIZE)); setParameters(next); }}>下一页</button>
          </nav>
        </>
      )}
    </main>
  );
}

export function RunEvidencePage() {
  const { runId = "", executionItemId } = useParams();
  return executionItemId
    ? <EvidenceDetail runId={runId} executionItemId={executionItemId} />
    : <EvidenceList runId={runId} />;
}
