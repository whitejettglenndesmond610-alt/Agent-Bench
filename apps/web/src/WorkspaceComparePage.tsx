import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  cancelWorkspaceAttempt,
  cancelWorkspaceComparison,
  ApiError,
  createEvaluationCase,
  createWorkspaceComparison,
  EvaluationCaseList,
  fetchWorkspaceAgents,
  fetchWorkspaceComparison,
  fetchWorkspaceComparisons,
  fetchWorkspaceExperienceTasks,
  fetchWorkspacePromptTemplates,
  updateWorkspaceComparison,
  WorkspaceAttempt,
  WorkspaceComparison,
  WorkspaceComparisonList,
  WorkspaceComparisonItem
} from "./api";
import { AgentPicker } from "./AgentPicker";
import { Button, Input, LoadingState, Modal } from "./components";
import { Citations, MarkdownContent } from "./WorkspacePage";
import {
  AttemptAnnotationControls,
  ClipboardButton,
  comparisonMarkdown,
  downloadJson,
  downloadMarkdown,
  userQueryKeys
} from "./userProduct";

const workspaceAgentsKey = (userId: string) => ["workspace", userId, "agents"] as const;
const workspaceComparisonsKey = (userId: string) => ["workspace", userId, "comparisons"] as const;
const workspaceComparisonKey = (userId: string, comparisonId: string) =>
  ["workspace", userId, "comparison", comparisonId] as const;
const workspaceEvaluationCasesKey = (userId: string) =>
  ["workspace", userId, "evaluation-cases"] as const;
const workspaceEvaluationCaseKey = (userId: string, caseId: string) =>
  ["workspace", userId, "evaluation-case", caseId] as const;
const activeComparisonStatuses = new Set(["queued", "running", "canceling"]);
const activeAttemptStatuses = new Set(["queued", "running"]);

const attemptLabels: Record<WorkspaceAttempt["status"], string> = {
  queued: "等待调度",
  running: "正在生成",
  succeeded: "已完成",
  failed: "失败",
  canceled: "已取消"
};

const comparisonLabels: Record<WorkspaceComparison["status"], string> = {
  queued: "等待调度",
  running: "运行中",
  canceling: "取消中",
  completed: "已完成"
};

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(date);
}

function metric(value: number | null): string {
  return value == null ? "--" : String(value);
}

function ComparisonHistory({
  comparisons,
  activeId,
  loading,
  error
}: {
  comparisons: WorkspaceComparison[];
  activeId: string | undefined;
  loading: boolean;
  error: string | null;
}) {
  return (
    <aside className="comparison-history" aria-label="对比历史">
      <header>
        <span>COMPARISON ARCHIVE</span>
        <strong>对比历史</strong>
        <Link to="/compare">+ 新建对比</Link>
      </header>
      {loading && <p className="comparison-history-state">正在读取历史记录</p>}
      {error && <p className="comparison-history-state is-error" role="alert">对比历史读取失败：{error}</p>}
      {!loading && !error && comparisons.length === 0 && <p className="comparison-history-state">暂无对比记录</p>}
      <ol>
        {comparisons.map((comparison) => (
          <li className={comparison.id === activeId ? "is-active" : ""} key={comparison.id}>
            <Link to={`/compare/${comparison.id}`}>
              <strong>{comparison.title}</strong>
              <span>{comparison.items.length} AGENTS · {comparisonLabels[comparison.status]}</span>
              <small>{comparison.counts.succeeded} 成功 / {comparison.counts.failed} 失败</small>
              <time dateTime={comparison.created_at}>{formatTime(comparison.created_at)}</time>
            </Link>
          </li>
        ))}
      </ol>
    </aside>
  );
}

function MetricsMatrix({ items }: { items: WorkspaceComparisonItem[] }) {
  const rows: Array<{ label: string; render: (item: WorkspaceComparisonItem) => string }> = [
    { label: "状态", render: (item) => attemptLabels[item.attempt.status] },
    { label: "延迟 (MS)", render: (item) => metric(item.attempt.latency_ms) },
    { label: "输入 TOKEN", render: (item) => metric(item.attempt.input_tokens) },
    { label: "输出 TOKEN", render: (item) => metric(item.attempt.output_tokens) },
    { label: "总 TOKEN", render: (item) => metric(item.attempt.total_tokens) },
    { label: "HTTP 状态", render: (item) => metric(item.attempt.last_http_status) },
    { label: "HTTP 调用", render: (item) => String(item.attempt.http_attempt_count) }
  ];
  return (
    <div className="workspace-compare-matrix">
      <table>
        <thead>
          <tr>
            <th>METRIC</th>
            {items.map((item) => <th key={item.position}>{item.agent.name}<small>V{item.agent.version}</small></th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.label}>
              <th>{row.label}</th>
              {items.map((item) => <td key={item.position}>{row.render(item)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AnswerCard({
  userId,
  item,
  canDraft,
  canceling,
  drafting,
  onCancel,
  onDraft
}: {
  userId: string;
  item: WorkspaceComparisonItem;
  canDraft: boolean;
  canceling: boolean;
  drafting: boolean;
  onCancel: () => void;
  onDraft: () => void;
}) {
  const { attempt } = item;
  const active = activeAttemptStatuses.has(attempt.status);
  return (
    <article className={`comparison-answer is-${attempt.status}`}>
      <header>
        <span>{String(item.position).padStart(2, "0")}</span>
        <div><strong>{item.agent.name}</strong><small>{item.agent.provider} / V{item.agent.version}</small></div>
        <b>{attemptLabels[attempt.status]}</b>
      </header>
      {attempt.status === "succeeded" && attempt.assistant_message ? (
        <>
          <MarkdownContent content={attempt.assistant_message.content} />
          <Citations citations={attempt.assistant_message.citations ?? []} />
          <AttemptAnnotationControls userId={userId} attempt={attempt} compact />
        </>
      ) : (
        <div className="comparison-answer-state" role={attempt.status === "failed" ? "alert" : "status"}>
          {active && <span className="status-dot" />}
          <strong>{attemptLabels[attempt.status]}</strong>
          {attempt.error && <small>{attempt.error.category} / {attempt.error.type}</small>}
        </div>
      )}
      <footer>
        <span>LAT {metric(attempt.latency_ms)} MS · TOKEN {metric(attempt.total_tokens)} · HTTP {metric(attempt.last_http_status)}</span>
        <div>
          {attempt.status === "succeeded" && attempt.assistant_message && <ClipboardButton text={attempt.assistant_message.content}>复制回答</ClipboardButton>}
          {active && (
            <button type="button" disabled={canceling || Boolean(attempt.cancel_requested_at)} onClick={onCancel}>
              {attempt.cancel_requested_at ? "停止中" : canceling ? "停止中" : "停止"}
            </button>
          )}
          {canDraft && (item.evaluation_case_id ? (
              <Link to={`/evaluation-cases/${item.evaluation_case_id}`}>查看评测草稿</Link>
            ) : attempt.status === "succeeded" ? (
              <button type="button" disabled={drafting} onClick={onDraft}>
                {drafting ? "保存中" : "保存为评测草稿"}
              </button>
            ) : null)}
        </div>
      </footer>
    </article>
  );
}

export function WorkspaceComparePage({ userId, canDraft }: { userId: string; canDraft: boolean }) {
  const { comparisonId } = useParams();
  const [searchParameters] = useSearchParams();
  const requestedAgentVersionId = searchParameters.get("agentVersion") ?? "";
  const requestedAgentIds = searchParameters.get("agents") ?? "";
  const requestedTemplateId = searchParameters.get("template") ?? "";
  const requestedTaskSlug = searchParameters.get("task") ?? "";
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [prompt, setPrompt] = useState("");
  const [selectedVersionIds, setSelectedVersionIds] = useState<string[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameTitle, setRenameTitle] = useState("");
  const selectionTouchedRef = useRef(false);
  const initialSelectionAppliedRef = useRef(false);
  const promptTouchedRef = useRef(false);
  const initialPromptAppliedRef = useRef(false);

  const agentsQuery = useQuery({
    queryKey: workspaceAgentsKey(userId),
    queryFn: fetchWorkspaceAgents,
    enabled: !comparisonId
  });
  const historyQuery = useQuery({
    queryKey: workspaceComparisonsKey(userId),
    queryFn: () => fetchWorkspaceComparisons(true),
    refetchInterval: (query) => query.state.data?.items.some(
      (item) => activeComparisonStatuses.has(item.status)
    ) ? 1_000 : false
  });
  const templatesQuery = useQuery({
    queryKey: userQueryKeys.templates(userId),
    queryFn: fetchWorkspacePromptTemplates,
    enabled: !comparisonId
  });
  const tasksQuery = useQuery({
    queryKey: userQueryKeys.tasks(userId),
    queryFn: fetchWorkspaceExperienceTasks,
    enabled: !comparisonId
  });
  const comparisonQuery = useQuery({
    queryKey: workspaceComparisonKey(userId, comparisonId ?? "new"),
    queryFn: () => fetchWorkspaceComparison(comparisonId!),
    enabled: Boolean(comparisonId),
    refetchInterval: (query) => {
      const comparison = query.state.data;
      return comparison && activeComparisonStatuses.has(comparison.status) ? 1_000 : false;
    }
  });

  useEffect(() => {
    if (comparisonId || initialSelectionAppliedRef.current || !agentsQuery.data) return;
    initialSelectionAppliedRef.current = true;
    if (selectionTouchedRef.current) return;
    const logicalAgentIds = requestedAgentIds.split(",").filter(Boolean);
    const requested = agentsQuery.data.filter((agent) =>
      agent.agent_version_id === requestedAgentVersionId || logicalAgentIds.includes(agent.agent_id)
    );
    const unique = requested.filter((agent, index) => requested.findIndex((item) => item.agent_id === agent.agent_id) === index).slice(0, 5);
    if (unique.length) setSelectedVersionIds(unique.map((agent) => agent.agent_version_id));
  }, [agentsQuery.data, comparisonId, requestedAgentIds, requestedAgentVersionId]);

  useEffect(() => {
    if (comparisonId || initialPromptAppliedRef.current) return;
    const template = requestedTemplateId
      ? templatesQuery.data?.items.find((item) => item.id === requestedTemplateId)
      : undefined;
    const task = requestedTaskSlug
      ? tasksQuery.data?.find((item) => item.slug === requestedTaskSlug)
      : undefined;
    const loading = (requestedTemplateId && templatesQuery.isPending) || (requestedTaskSlug && tasksQuery.isPending);
    if (loading) return;
    initialPromptAppliedRef.current = true;
    if (!promptTouchedRef.current && !prompt) setPrompt(template?.content ?? task?.prompt ?? "");
  }, [comparisonId, prompt, requestedTaskSlug, requestedTemplateId, tasksQuery.data, tasksQuery.isPending, templatesQuery.data, templatesQuery.isPending]);

  useEffect(() => {
    const updated = comparisonQuery.data;
    if (!updated) return;
    queryClient.setQueryData<WorkspaceComparisonList>(workspaceComparisonsKey(userId), (current) => {
      if (!current) return current;
      return {
        items: current.items.map((item) => item.id === updated.id ? updated : item)
      };
    });
  }, [comparisonQuery.data, queryClient, userId]);

  const createMutation = useMutation({
    mutationFn: () => createWorkspaceComparison({
      prompt: prompt.trim(),
      agent_version_ids: selectedVersionIds,
      idempotency_key: crypto.randomUUID()
    }),
    onMutate: () => setActionError(null),
    onSuccess: (comparison) => {
      queryClient.setQueryData(workspaceComparisonKey(userId, comparison.id), comparison);
      void queryClient.invalidateQueries({ queryKey: workspaceComparisonsKey(userId) });
      navigate(`/compare/${comparison.id}`);
    },
    onError: (error) => setActionError(error instanceof Error ? error.message : "创建并行对比失败")
  });

  function replaceAttempt(comparison: WorkspaceComparison | undefined, attempt: WorkspaceAttempt) {
    if (!comparison) return comparison;
    return {
      ...comparison,
      items: comparison.items.map((item) => item.attempt.id === attempt.id ? { ...item, attempt } : item)
    };
  }

  const cancelItemMutation = useMutation({
    mutationFn: ({ attemptId }: { attemptId: string; originComparisonId: string }) =>
      cancelWorkspaceAttempt(attemptId),
    onMutate: () => setActionError(null),
    onSuccess: (attempt, variables) => {
      queryClient.setQueryData<WorkspaceComparison>(
        workspaceComparisonKey(userId, variables.originComparisonId),
        (current) => replaceAttempt(current, attempt)
      );
      void queryClient.invalidateQueries({
        queryKey: workspaceComparisonKey(userId, variables.originComparisonId)
      });
      void queryClient.invalidateQueries({ queryKey: workspaceComparisonsKey(userId) });
    },
    onError: (error, variables) => {
      if (comparisonId === variables.originComparisonId) {
        setActionError(error instanceof Error ? error.message : "停止生成失败");
      }
    }
  });

  const cancelAllMutation = useMutation({
    mutationFn: ({ originComparisonId }: { originComparisonId: string }) =>
      cancelWorkspaceComparison(originComparisonId),
    onMutate: () => setActionError(null),
    onSuccess: (updated, variables) => {
      queryClient.setQueryData(
        workspaceComparisonKey(userId, variables.originComparisonId),
        updated
      );
      void queryClient.invalidateQueries({
        queryKey: workspaceComparisonKey(userId, variables.originComparisonId)
      });
      void queryClient.invalidateQueries({ queryKey: workspaceComparisonsKey(userId) });
    },
    onError: (error, variables) => {
      if (comparisonId === variables.originComparisonId) {
        setActionError(error instanceof Error ? error.message : "取消全部生成失败");
      }
    }
  });

  const createDraftMutation = useMutation({
    mutationFn: ({ attemptId }: { attemptId: string; originComparisonId: string }) => createEvaluationCase({
      source_attempt_id: attemptId,
      idempotency_key: crypto.randomUUID()
    }),
    onMutate: () => setActionError(null),
    onSuccess: (evaluationCase, variables) => {
      queryClient.setQueryData(workspaceEvaluationCaseKey(userId, evaluationCase.id), evaluationCase);
      queryClient.setQueryData<EvaluationCaseList>(workspaceEvaluationCasesKey(userId), (current) => {
        if (!current) return { items: [evaluationCase] };
        const exists = current.items.some((item) => item.id === evaluationCase.id);
        return {
          items: exists
            ? current.items.map((item) => item.id === evaluationCase.id ? evaluationCase : item)
            : [evaluationCase, ...current.items]
        };
      });
      void queryClient.invalidateQueries({
        queryKey: workspaceEvaluationCasesKey(userId),
        refetchType: "none"
      });
      queryClient.setQueryData<WorkspaceComparison>(
        workspaceComparisonKey(userId, variables.originComparisonId),
        (current) => current ? {
          ...current,
          items: current.items.map((item) => item.attempt.id === variables.attemptId
            ? { ...item, evaluation_case_id: evaluationCase.id }
            : item)
        } : current
      );
      void queryClient.invalidateQueries({
        queryKey: workspaceComparisonKey(userId, variables.originComparisonId)
      });
      void queryClient.invalidateQueries({ queryKey: workspaceComparisonsKey(userId) });
    },
    onError: (error, variables) => {
      if (comparisonId === variables.originComparisonId) {
        setActionError(error instanceof Error ? error.message : "保存评测草稿失败");
      }
    }
  });

  const updateMutation = useMutation({
    mutationFn: ({ source, title, archived }: { source: WorkspaceComparison; title?: string; archived?: boolean }) =>
      updateWorkspaceComparison(source.id, {
        expected_revision: source.revision ?? 0,
        ...(title === undefined ? {} : { title }),
        ...(archived === undefined ? {} : { archived })
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData(workspaceComparisonKey(userId, updated.id), updated);
      queryClient.setQueryData<WorkspaceComparisonList>(workspaceComparisonsKey(userId), (current) => ({
        items: current?.items.map((item) => item.id === updated.id ? updated : item) ?? [updated]
      }));
      setRenameOpen(false);
      setActionError(null);
    },
    onError: (error, variables) => {
      setActionError(error instanceof ApiError && error.code === "revision_conflict"
        ? "对比记录已在其他位置更新，正在刷新，请重新操作。"
        : error instanceof Error ? error.message : "更新对比失败");
      if (error instanceof ApiError && error.code === "revision_conflict") {
        void queryClient.invalidateQueries({ queryKey: workspaceComparisonKey(userId, variables.source.id) });
        void queryClient.invalidateQueries({ queryKey: workspaceComparisonsKey(userId) });
      }
    }
  });

  const rerunMutation = useMutation({
    mutationFn: (source: WorkspaceComparison) => createWorkspaceComparison({
      prompt: source.prompt,
      agent_version_ids: [...source.items].sort((left, right) => left.position - right.position).map((item) => item.agent.agent_version_id),
      title: `${source.title} · 再次运行`,
      idempotency_key: crypto.randomUUID()
    }),
    onSuccess: (created) => {
      queryClient.setQueryData(workspaceComparisonKey(userId, created.id), created);
      void queryClient.invalidateQueries({ queryKey: workspaceComparisonsKey(userId) });
      navigate(`/compare/${created.id}`);
    },
    onError: (error) => setActionError(error instanceof Error ? error.message : "再次运行失败")
  });

  function submitComparison(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!prompt.trim() || selectedVersionIds.length < 2 || selectedVersionIds.length > 5) return;
    createMutation.mutate();
  }

  const comparison = comparisonQuery.data;
  const sortedItems = comparison ? [...comparison.items].sort((left, right) => left.position - right.position) : [];
  const active = comparison ? activeComparisonStatuses.has(comparison.status) : false;

  return (
    <main className="workspace workspace-compare-page">
      <header className="workspace-compare-header">
        <div>
          <p className="eyebrow">DURABLE MULTI-AGENT WORKSPACE</p>
          <h1>{comparison ? comparison.title : "并行对比"}</h1>
          <p>同一提示词进入独立 Agent 链路，回答与运行指标持续写入服务端。</p>
        </div>
        <Link className="back-link" to="/workspace">← 单 Agent 对话</Link>
      </header>

      {actionError && <div className="result-banner is-error" role="alert">{actionError}</div>}
      <div className="workspace-compare-layout">
        <ComparisonHistory
          comparisons={historyQuery.data?.items ?? []}
          activeId={comparisonId}
          loading={historyQuery.isPending}
          error={historyQuery.error?.message ?? null}
        />

        {!comparisonId ? (
          <form className="comparison-builder" onSubmit={submitComparison}>
            <fieldset>
              <legend>选择 Agent <span>{selectedVersionIds.length} / 5</span></legend>
              <AgentPicker
                agents={agentsQuery.data ?? []}
                selectedVersionIds={selectedVersionIds}
                recentVersionIds={(historyQuery.data?.items ?? []).flatMap((item) =>
                  [...item.items]
                    .sort((left, right) => left.position - right.position)
                    .map((entry) => entry.agent.agent_version_id)
                )}
                mode="multiple"
                maxSelected={5}
                loading={agentsQuery.isPending}
                error={agentsQuery.error?.message ?? null}
                onChange={(value) => { selectionTouchedRef.current = true; setSelectedVersionIds(value); }}
              />
              {!agentsQuery.isPending && (agentsQuery.data?.length ?? 0) === 0 && (
                <p className="comparison-builder-state">当前账号没有可用的 Agent 版本。</p>
              )}
            </fieldset>
            <div className="comparison-resource-pickers">
              <label><span>Prompt 模板</span><select aria-label="选择 Prompt 模板" value="" disabled={templatesQuery.isPending || !templatesQuery.data?.items.length} onChange={(event) => {
                const template = templatesQuery.data?.items.find((item) => item.id === event.target.value);
                if (!template) return;
                if (prompt || promptTouchedRef.current) { setActionError("为避免覆盖已输入内容，请先清空提示词再选择模板。"); return; }
                promptTouchedRef.current = true;
                setPrompt(template.content);
              }}><option value="">选择模板填入空白提示词</option>{templatesQuery.data?.items.map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}</select><Link to="/templates">管理模板</Link></label>
              <label><span>体验任务</span><select aria-label="选择体验任务" value="" disabled={tasksQuery.isPending || !tasksQuery.data?.length} onChange={(event) => {
                const task = tasksQuery.data?.find((item) => item.slug === event.target.value);
                if (!task) return;
                if (prompt || promptTouchedRef.current) { setActionError("为避免覆盖已输入内容，请先清空提示词再选择任务。"); return; }
                promptTouchedRef.current = true;
                setPrompt(task.prompt);
              }}><option value="">选择非排名体验任务</option>{tasksQuery.data?.map((task) => <option key={task.slug} value={task.slug}>{task.title}</option>)}</select><Link to="/experience">查看任务</Link></label>
            </div>
            <label className="comparison-prompt-field">
              <span>统一提示词</span>
              <textarea
                aria-label="统一提示词"
                rows={8}
                value={prompt}
                maxLength={200_000}
                placeholder="输入将同时发送给所选 Agent 的问题"
                onChange={(event) => { promptTouchedRef.current = true; setPrompt(event.target.value); }}
              />
              <small>{prompt.length} 字 · 选择 2 至 5 个 Agent 后批量提交</small>
            </label>
            <button className="button" type="submit" disabled={createMutation.isPending || !prompt.trim() || selectedVersionIds.length < 2}>
              {createMutation.isPending ? "正在建立对比" : `启动 ${selectedVersionIds.length || "--"} 路并行对比`}
            </button>
          </form>
        ) : comparisonQuery.isPending ? (
          <LoadingState label="正在装载对比账本" />
        ) : comparisonQuery.error || !comparison ? (
          <div className="workspace-detail-state is-error"><strong>无法读取此对比</strong><span>{comparisonQuery.error?.message ?? "对比不存在"}</span></div>
        ) : (
          <section className="comparison-detail">
            <nav className="comparison-detail-actions" aria-label="对比记录操作">
              <button type="button" onClick={() => { setRenameTitle(comparison.title); setRenameOpen(true); }}>重命名</button>
              <button type="button" disabled={updateMutation.isPending} onClick={() => updateMutation.mutate({ source: comparison, archived: !comparison.archived_at })}>{comparison.archived_at ? "恢复" : "归档"}</button>
              <button type="button" disabled={rerunMutation.isPending} onClick={() => rerunMutation.mutate(comparison)}>{rerunMutation.isPending ? "创建中" : "再次运行"}</button>
              <button type="button" onClick={() => downloadMarkdown(comparison.title, comparisonMarkdown(comparison))}>导出 Markdown</button>
              <button type="button" onClick={() => downloadJson(comparison.title, comparison)}>导出 JSON</button>
              {comparison.items.length >= 2 && comparison.items.length <= 5 && <Link to={`/leaderboard/compare?agents=${comparison.items.map((item) => item.agent.agent_id).join(",")}`}>对比正式成绩</Link>}
            </nav>
            <header className="comparison-detail-status">
              <div><span>STATUS</span><strong>{comparisonLabels[comparison.status]}</strong></div>
              <dl>
                <div><dt>成功</dt><dd>{comparison.counts.succeeded}</dd></div>
                <div><dt>失败</dt><dd>{comparison.counts.failed}</dd></div>
                <div><dt>取消</dt><dd>{comparison.counts.canceled}</dd></div>
              </dl>
              {active && (
                <button
                  className="button is-danger"
                  type="button"
                  disabled={
                    (cancelAllMutation.isPending
                      && cancelAllMutation.variables?.originComparisonId === comparison.id)
                    || Boolean(comparison.cancel_requested_at)
                  }
                  onClick={() => cancelAllMutation.mutate({ originComparisonId: comparison.id })}
                >
                  {comparison.cancel_requested_at ? "全部取消中" : "取消全部"}
                </button>
              )}
            </header>
            <section className="comparison-prompt-readonly">
              <span>PROMPT / IMMUTABLE</span>
              <p>{comparison.prompt}</p>
            </section>
            <MetricsMatrix items={sortedItems} />
            <div className="comparison-answer-grid">
              {sortedItems.map((item) => (
                <AnswerCard
                  key={item.position}
                  userId={userId}
                  item={item}
                  canDraft={canDraft}
                  canceling={
                    cancelItemMutation.isPending
                    && cancelItemMutation.variables?.attemptId === item.attempt.id
                    && cancelItemMutation.variables?.originComparisonId === comparison.id
                  }
                  drafting={
                    createDraftMutation.isPending
                    && createDraftMutation.variables?.attemptId === item.attempt.id
                    && createDraftMutation.variables?.originComparisonId === comparison.id
                  }
                  onCancel={() => cancelItemMutation.mutate({
                    attemptId: item.attempt.id,
                    originComparisonId: comparison.id
                  })}
                  onDraft={() => createDraftMutation.mutate({
                    attemptId: item.attempt.id,
                    originComparisonId: comparison.id
                  })}
                />
              ))}
            </div>
          </section>
        )}
      </div>
      {renameOpen && comparison && <Modal title="重命名对比" description="更新标题不会改变提示词或 Agent 版本。" eyebrow="RENAME COMPARISON" onClose={() => setRenameOpen(false)}><form className="modal-form" onSubmit={(event) => { event.preventDefault(); if (renameTitle.trim()) updateMutation.mutate({ source: comparison, title: renameTitle.trim() }); }}><label className="form-field"><span>对比标题</span><Input aria-label="对比标题" maxLength={200} autoFocus value={renameTitle} onChange={(event) => setRenameTitle(event.target.value)} /></label>{actionError && <div className="result-banner is-error" role="alert">{actionError}</div>}<div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => setRenameOpen(false)}>取消</Button><Button type="submit" disabled={updateMutation.isPending || !renameTitle.trim()}>{updateMutation.isPending ? "保存中" : "保存标题"}</Button></div></form></Modal>}
    </main>
  );
}
