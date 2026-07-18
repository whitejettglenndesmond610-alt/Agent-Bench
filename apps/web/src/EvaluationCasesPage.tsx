import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  Ability,
  ApiError,
  EvaluationCase,
  EvaluationCaseList,
  EvaluationQuestionType,
  fetchEvaluationCase,
  fetchEvaluationCases,
  publishEvaluationCase,
  reviewEvaluationCase,
  updateEvaluationCase
} from "./api";
import { Citations, MarkdownContent } from "./WorkspacePage";
import { abilityMetrics } from "./metrics";

const evaluationCasesKey = (userId: string) => ["workspace", userId, "evaluation-cases"] as const;
const evaluationCaseKey = (userId: string, caseId: string) =>
  ["workspace", userId, "evaluation-case", caseId] as const;

const statusLabels: Record<EvaluationCase["status"], string> = {
  draft: "草稿",
  reviewed: "已复核",
  published: "已发布"
};

function formatTime(value: string | null): string {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(date);
}

function optionalText(value: string): string | null {
  return value.trim() || null;
}

function CaseHistory({
  cases,
  activeId,
  loading,
  error
}: {
  cases: EvaluationCase[];
  activeId: string | undefined;
  loading: boolean;
  error: string | null;
}) {
  return (
    <aside className="evaluation-case-history" aria-label="评测案例历史">
      <header><span>CURATION ARCHIVE</span><strong>案例历史</strong></header>
      {loading && <p>正在读取案例</p>}
      {error && <p className="is-error" role="alert">案例历史读取失败：{error}</p>}
      {!loading && !error && cases.length === 0 && <p>暂无评测草稿</p>}
      <ol>
        {cases.map((item) => (
          <li className={item.id === activeId ? "is-active" : ""} key={item.id}>
            <Link to={`/evaluation-cases/${item.id}`}>
              <strong>{item.prompt}</strong>
              <span className={`case-status is-${item.status}`}>{statusLabels[item.status]}</span>
              <small>{item.source.agent.name} · REV {item.revision}</small>
              <time dateTime={item.updated_at}>{formatTime(item.updated_at)}</time>
            </Link>
          </li>
        ))}
      </ol>
    </aside>
  );
}

function SourceRecord({ evaluationCase }: { evaluationCase: EvaluationCase }) {
  const source = evaluationCase.source;
  return (
    <section className="evaluation-source-record">
      <header>
        <div><span>IMMUTABLE SOURCE</span><strong>候选回答与生成记录</strong></div>
        <b>{source.agent.name} / V{source.agent.version}</b>
      </header>
      <div className="source-question">
        <span>ORIGINAL USER MESSAGE</span>
        <p>{source.user_message}</p>
      </div>
      <div className="source-candidate">
        <span>CANDIDATE ANSWER</span>
        <MarkdownContent content={source.candidate_answer} />
        <Citations citations={source.citations} />
      </div>
      <dl>
        <div><dt>延迟</dt><dd>{source.latency_ms == null ? "--" : `${source.latency_ms} MS`}</dd></div>
        <div><dt>输入 TOKEN</dt><dd>{source.input_tokens ?? "--"}</dd></div>
        <div><dt>输出 TOKEN</dt><dd>{source.output_tokens ?? "--"}</dd></div>
        <div><dt>总 TOKEN</dt><dd>{source.total_tokens ?? "--"}</dd></div>
        <div><dt>生成序号</dt><dd>{source.generation_no}</dd></div>
        <div><dt>PROMPT FORMAT</dt><dd>{source.prompt_format}</dd></div>
      </dl>
      <details>
        <summary>完整性哈希</summary>
        <code>SOURCE {evaluationCase.source_hash}</code>
        <code>PROMPT {source.prompt_hash}</code>
        <code>ARTIFACT {source.artifact_sha256 ?? "--"}</code>
      </details>
    </section>
  );
}

function CaseEditor({
  evaluationCase,
  canPublish,
  onUpdated,
  onRevisionConflict
}: {
  evaluationCase: EvaluationCase;
  canPublish: boolean;
  onUpdated: (updated: EvaluationCase) => void;
  onRevisionConflict: () => void;
}) {
  const immutable = evaluationCase.status === "published";
  const [prompt, setPrompt] = useState(evaluationCase.prompt);
  const [referenceAnswer, setReferenceAnswer] = useState(evaluationCase.reference_answer ?? "");
  const [language, setLanguage] = useState<"" | "zh" | "en">(evaluationCase.language ?? "");
  const [ability, setAbility] = useState<"" | Ability>(evaluationCase.ability ?? "");
  const [questionType, setQuestionType] = useState<EvaluationQuestionType>(evaluationCase.question_type);
  const [requiresCitations, setRequiresCitations] = useState(evaluationCase.requires_citations);
  const [difficulty, setDifficulty] = useState(evaluationCase.difficulty ?? "");
  const [domain, setDomain] = useState(evaluationCase.domain ?? "");
  const [reviewNotes, setReviewNotes] = useState(evaluationCase.review_notes ?? "");
  const [standaloneVerified, setStandaloneVerified] = useState(evaluationCase.standalone_verified);
  const [referenceVerified, setReferenceVerified] = useState(evaluationCase.reference_answer_verified);
  const [rightsVerified, setRightsVerified] = useState(evaluationCase.rights_verified);
  const [sensitiveVerified, setSensitiveVerified] = useState(evaluationCase.sensitive_data_verified);
  const [publishConfirmed, setPublishConfirmed] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  function handleError(error: unknown, fallback: string) {
    if (error instanceof ApiError && error.status === 409 && error.code === "revision_conflict") {
      onRevisionConflict();
      return;
    }
    setActionError(error instanceof Error ? error.message : fallback);
  }

  const saveMutation = useMutation({
    mutationFn: () => updateEvaluationCase(evaluationCase.id, {
      expected_revision: evaluationCase.revision,
      prompt: prompt.trim(),
      reference_answer: optionalText(referenceAnswer),
      language: language || null,
      ability: ability || null,
      question_type: questionType,
      requires_citations: requiresCitations,
      difficulty: optionalText(difficulty),
      domain: optionalText(domain),
      review_notes: optionalText(reviewNotes)
    }),
    onMutate: () => setActionError(null),
    onSuccess: onUpdated,
    onError: (error) => handleError(error, "保存案例失败")
  });

  const reviewMutation = useMutation({
    mutationFn: () => reviewEvaluationCase(evaluationCase.id, {
      expected_revision: evaluationCase.revision,
      standalone_verified: true,
      reference_answer_verified: true,
      rights_verified: true,
      sensitive_data_verified: true
    }),
    onMutate: () => setActionError(null),
    onSuccess: onUpdated,
    onError: (error) => handleError(error, "复核案例失败")
  });

  const publishMutation = useMutation({
    mutationFn: () => publishEvaluationCase(evaluationCase.id, {
      expected_revision: evaluationCase.revision,
      confirm_curated_only: true
    }),
    onMutate: () => setActionError(null),
    onSuccess: onUpdated,
    onError: (error) => handleError(error, "发布案例失败")
  });

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!prompt.trim() || immutable || !dirty) return;
    saveMutation.mutate();
  }

  const dirty = (
    prompt.trim() !== evaluationCase.prompt
    || optionalText(referenceAnswer) !== evaluationCase.reference_answer
    || (language || null) !== evaluationCase.language
    || (ability || null) !== evaluationCase.ability
    || questionType !== evaluationCase.question_type
    || requiresCitations !== evaluationCase.requires_citations
    || optionalText(difficulty) !== evaluationCase.difficulty
    || optionalText(domain) !== evaluationCase.domain
    || optionalText(reviewNotes) !== evaluationCase.review_notes
  );
  const allVerified = standaloneVerified && referenceVerified && rightsVerified && sensitiveVerified;
  const busy = saveMutation.isPending || reviewMutation.isPending || publishMutation.isPending;
  const dirtyHintId = `case-dirty-${evaluationCase.id}`;

  return (
    <div className="evaluation-case-editor">
      <header className="case-editor-header">
        <div>
          <span>CASE {evaluationCase.id.slice(0, 8).toUpperCase()}</span>
          <h2>评测案例编辑器</h2>
          <small>REV {evaluationCase.revision} · 更新于 {formatTime(evaluationCase.updated_at)}</small>
        </div>
        <b className={`case-status is-${evaluationCase.status}`}>{statusLabels[evaluationCase.status]}</b>
      </header>

      {actionError && <div className="result-banner is-error" role="alert">{actionError}</div>}
      {immutable && <div className="published-immutable-notice">已发布案例不可修改。内容和记录哈希已锁定。</div>}

      <div className="evaluation-editor-columns">
        <form className="evaluation-edit-form" onSubmit={save}>
          <label>
            <span>评测问题</span>
            <textarea rows={8} value={prompt} disabled={immutable} maxLength={200_000} onChange={(event) => setPrompt(event.target.value)} />
          </label>
          <label>
            <span>参考答案</span>
            <textarea
              rows={9}
              value={referenceAnswer}
              disabled={immutable}
              maxLength={200_000}
              placeholder="从空白开始撰写独立参考答案"
              onChange={(event) => setReferenceAnswer(event.target.value)}
            />
          </label>
          <div className="evaluation-field-grid">
            <label>
              <span>语言</span>
              <select value={language} disabled={immutable} onChange={(event) => setLanguage(event.target.value as "" | "zh" | "en")}>
                <option value="">未设置</option>
                <option value="zh">中文</option>
                <option value="en">英文</option>
              </select>
            </label>
            <label>
              <span>能力维度</span>
              <select value={ability} disabled={immutable} onChange={(event) => setAbility(event.target.value as "" | Ability)}>
                <option value="">未设置</option>
                {abilityMetrics.map((item) => <option value={item.key} key={item.key}>{item.label}</option>)}
              </select>
            </label>
            <label>
              <span>题型</span>
              <select value={questionType} disabled={immutable} onChange={(event) => setQuestionType(event.target.value as EvaluationQuestionType)}>
                <option value="short_answer">简短回答</option>
                <option value="open_ended">开放回答</option>
              </select>
            </label>
            <label className="case-checkbox-field">
              <input type="checkbox" checked={requiresCitations} disabled={immutable} onChange={(event) => setRequiresCitations(event.target.checked)} />
              <span>要求引用来源</span>
            </label>
            <label>
              <span>难度</span>
              <input value={difficulty} disabled={immutable} maxLength={80} onChange={(event) => setDifficulty(event.target.value)} />
            </label>
            <label>
              <span>领域</span>
              <input value={domain} disabled={immutable} maxLength={120} onChange={(event) => setDomain(event.target.value)} />
            </label>
          </div>
          <label>
            <span>复核备注</span>
            <textarea rows={5} value={reviewNotes} disabled={immutable} maxLength={10_000} onChange={(event) => setReviewNotes(event.target.value)} />
          </label>
          {!immutable && <button className="button" type="submit" disabled={busy || !dirty || !prompt.trim()}>{saveMutation.isPending ? "保存中" : "保存修改"}</button>}
        </form>
        <SourceRecord evaluationCase={evaluationCase} />
      </div>

      {!immutable && evaluationCase.status === "draft" && (
        <section className="case-review-gate">
          <header>
            <span>EXPLICIT REVIEW GATE</span><h3>提交复核</h3>
            <p>四项确认必须分别勾选，且参考答案、语言和能力维度必须完整。</p>
            {dirty && <small id={dirtyHintId} className="case-save-first-hint" role="status" aria-live="polite">有未保存修改，请先保存修改。</small>}
          </header>
          <div>
            <label><input type="checkbox" checked={standaloneVerified} onChange={(event) => setStandaloneVerified(event.target.checked)} />案例可脱离原对话独立理解</label>
            <label><input type="checkbox" checked={referenceVerified} onChange={(event) => setReferenceVerified(event.target.checked)} />参考答案已经人工核验</label>
            <label><input type="checkbox" checked={rightsVerified} onChange={(event) => setRightsVerified(event.target.checked)} />内容来源与使用权已确认</label>
            <label><input type="checkbox" checked={sensitiveVerified} onChange={(event) => setSensitiveVerified(event.target.checked)} />不包含敏感或个人数据</label>
          </div>
          <button
            className="button is-quiet"
            type="button"
            disabled={busy || dirty || !allVerified}
            aria-describedby={dirty ? dirtyHintId : undefined}
            onClick={() => { if (!dirty) reviewMutation.mutate(); }}
          >
            {reviewMutation.isPending ? "复核中" : "确认复核"}
          </button>
        </section>
      )}

      {!immutable && evaluationCase.status === "reviewed" && (
        <section className="case-publish-gate">
          <header>
            <span>CURATED LIBRARY PUBLICATION</span><h3>发布精选案例</h3>
            {dirty && <small id={dirtyHintId} className="case-save-first-hint" role="status" aria-live="polite">有未保存修改，请先保存修改。</small>}
          </header>
          <p><strong>发布仅进入精选案例库。</strong>不会进入正式数据集、评测运行或排行榜。</p>
          {canPublish ? (
            <>
              <label>
                <input type="checkbox" checked={publishConfirmed} onChange={(event) => setPublishConfirmed(event.target.checked)} />
                我确认本次发布仅用于精选案例库，不进入正式评测链路
              </label>
              <button
                className="button is-danger"
                type="button"
                disabled={busy || dirty || !publishConfirmed}
                aria-describedby={dirty ? dirtyHintId : undefined}
                onClick={() => { if (!dirty) publishMutation.mutate(); }}
              >
                {publishMutation.isPending ? "发布中" : "发布到精选案例库"}
              </button>
            </>
          ) : <small>共享运营会话不能发布；请使用具名运营账号完成最终发布。</small>}
        </section>
      )}
    </div>
  );
}

export function EvaluationCasesPage({ userId, canPublish }: { userId: string; canPublish: boolean }) {
  const { caseId } = useParams();
  const queryClient = useQueryClient();
  const [pageError, setPageError] = useState<string | null>(null);
  const casesQuery = useQuery({
    queryKey: evaluationCasesKey(userId),
    queryFn: fetchEvaluationCases
  });
  const caseQuery = useQuery({
    queryKey: evaluationCaseKey(userId, caseId ?? "none"),
    queryFn: () => fetchEvaluationCase(caseId!),
    enabled: Boolean(caseId)
  });

  function updateCaches(updated: EvaluationCase) {
    setPageError(null);
    queryClient.setQueryData(evaluationCaseKey(userId, updated.id), updated);
    queryClient.setQueryData<EvaluationCaseList>(evaluationCasesKey(userId), (current) => {
      if (!current) return { items: [updated] };
      const exists = current.items.some((item) => item.id === updated.id);
      return { items: exists ? current.items.map((item) => item.id === updated.id ? updated : item) : [updated, ...current.items] };
    });
  }

  async function refetchConflict() {
    setPageError("案例已在其他位置更新，正在重新读取最新版本。");
    if (caseId) {
      await Promise.all([
        queryClient.refetchQueries({ queryKey: evaluationCaseKey(userId, caseId), exact: true }),
        queryClient.refetchQueries({ queryKey: evaluationCasesKey(userId), exact: true })
      ]);
    }
    setPageError("案例已在其他位置更新，已重新读取最新版本，请检查后重试。");
  }

  const evaluationCase = caseQuery.data;

  return (
    <main className="workspace evaluation-cases-page">
      <header className="evaluation-cases-header">
        <div><p className="eyebrow">CURATED EVALUATION CASES</p><h1>精选评测案例</h1><p>从真实 Agent 回答建立独立、可复核、可追溯的精选案例。</p></div>
        <Link className="back-link" to="/workspace">← 返回工作台</Link>
      </header>
      {pageError && <div className="result-banner is-error" role="alert">{pageError}</div>}
      <div className="evaluation-cases-layout">
        <CaseHistory
          cases={casesQuery.data?.items ?? []}
          activeId={caseId}
          loading={casesQuery.isPending}
          error={casesQuery.error?.message ?? null}
        />
        {!caseId ? (
          <section className="evaluation-case-placeholder"><span>NO CASE SELECTED</span><h2>选择一条案例开始编辑</h2><p>新的草稿从工作台中的成功回答创建。</p></section>
        ) : caseQuery.isPending ? (
          <div className="workspace-detail-state"><span className="status-dot" />正在装载案例</div>
        ) : caseQuery.error || !evaluationCase ? (
          <div className="workspace-detail-state is-error"><strong>无法读取此案例</strong><span>{caseQuery.error?.message ?? "案例不存在"}</span></div>
        ) : (
          <CaseEditor
            key={`${evaluationCase.id}-${evaluationCase.revision}`}
            evaluationCase={evaluationCase}
            canPublish={canPublish}
            onUpdated={updateCaches}
            onRevisionConflict={() => void refetchConflict()}
          />
        )}
      </div>
    </main>
  );
}
