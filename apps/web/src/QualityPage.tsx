import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  BadPatternFindingStatus,
  BadPatternSeverity,
  QualityReviewVerdict,
  QualityTaskPriority,
  QualityTaskStatus,
  batchAssignQualityTasks,
  batchReviewQualityFindings,
  claimQualityTask,
  createBadPatternRule,
  fetchBadPatternRules,
  fetchQualityFeedbackExport,
  fetchQualityDashboard,
  fetchQualityFinding,
  fetchQualityReviewers,
  rescanQualityExecutionItem,
  reviewQualityFinding,
  updateBadPatternRule,
  upsertQualityReview
} from "./api";
import { Button, FormField, Input, LoadingState, Select, Textarea } from "./components";
import { downloadText } from "./userProduct";

const severityLabels: Record<BadPatternSeverity, string> = {
  critical: "严重",
  high: "高危",
  medium: "中危",
  low: "低危"
};

const statusLabels: Record<BadPatternFindingStatus, string> = {
  open: "待处理",
  confirmed: "已确认",
  dismissed: "已排除"
};

const verdictLabels: Record<QualityReviewVerdict, string> = {
  pass: "质检通过",
  fail: "质检不通过",
  needs_followup: "需要跟进"
};

const taskStatusLabels: Record<QualityTaskStatus, string> = {
  unassigned: "待分配",
  assigned: "已分配",
  in_review: "质检中",
  completed: "已完成"
};

const priorityLabels: Record<QualityTaskPriority, string> = {
  low: "低",
  normal: "普通",
  high: "高",
  urgent: "紧急"
};

function formatDate(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

export function QualityPage() {
  const { findingId } = useParams();
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<BadPatternFindingStatus | "">("open");
  const [severity, setSeverity] = useState<BadPatternSeverity | "">("");
  const [taskStatus, setTaskStatus] = useState<QualityTaskStatus | "">("");
  const [assigneeUserId, setAssigneeUserId] = useState("");
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [reviewTags, setReviewTags] = useState("");
  const [showRuleForm, setShowRuleForm] = useState(false);
  const [selectedFindingIds, setSelectedFindingIds] = useState<Set<string>>(new Set());
  const [batchAssigneeUserId, setBatchAssigneeUserId] = useState("");
  const [batchPriority, setBatchPriority] = useState<QualityTaskPriority>("normal");
  const [batchNote, setBatchNote] = useState("");

  const dashboardQuery = useQuery({
    queryKey: ["quality-dashboard", status, severity, taskStatus, assigneeUserId, search],
    queryFn: () => fetchQualityDashboard({
      ...(status ? { status } : {}),
      ...(severity ? { severity } : {}),
      ...(taskStatus ? { task_status: taskStatus } : {}),
      ...(assigneeUserId ? { assignee_user_id: assigneeUserId } : {}),
      ...(search ? { search } : {})
    }),
    refetchInterval: 15_000
  });
  const detailQuery = useQuery({
    queryKey: ["quality-finding", findingId],
    queryFn: () => fetchQualityFinding(findingId as string),
    enabled: Boolean(findingId)
  });
  const rulesQuery = useQuery({ queryKey: ["bad-pattern-rules"], queryFn: fetchBadPatternRules });
  const reviewersQuery = useQuery({ queryKey: ["quality-reviewers"], queryFn: fetchQualityReviewers });

  useEffect(() => {
    const review = detailQuery.data?.quality_review;
    setReviewNote(review?.note ?? detailQuery.data?.review_note ?? "");
    setReviewTags(review?.tags.join(", ") ?? "");
  }, [detailQuery.data?.id, detailQuery.data?.quality_review, detailQuery.data?.review_note]);

  async function refreshQuality() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["quality-dashboard"] }),
      queryClient.invalidateQueries({ queryKey: ["quality-finding"] }),
      queryClient.invalidateQueries({ queryKey: ["bad-pattern-rules"] })
    ]);
  }

  const findingMutation = useMutation({
    mutationFn: (nextStatus: Exclude<BadPatternFindingStatus, "open">) =>
      reviewQualityFinding(findingId as string, { status: nextStatus, note: reviewNote }),
    onSuccess: refreshQuality
  });
  const qualityReviewMutation = useMutation({
    mutationFn: (verdict: QualityReviewVerdict) => upsertQualityReview(
      detailQuery.data?.execution_item_id as string,
      {
        verdict,
        tags: reviewTags.split(",").map((tag) => tag.trim()).filter(Boolean),
        note: reviewNote
      }
    ),
    onSuccess: refreshQuality
  });
  const rescanMutation = useMutation({
    mutationFn: () => rescanQualityExecutionItem(detailQuery.data?.execution_item_id as string),
    onSuccess: refreshQuality
  });
  const toggleRuleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      updateBadPatternRule(id, { enabled }),
    onSuccess: refreshQuality
  });
  const batchAssignMutation = useMutation({
    mutationFn: () => batchAssignQualityTasks({
      finding_ids: Array.from(selectedFindingIds),
      assignee_user_id: batchAssigneeUserId || null,
      priority: batchPriority
    }),
    onSuccess: async () => { setSelectedFindingIds(new Set()); await refreshQuality(); }
  });
  const batchReviewMutation = useMutation({
    mutationFn: (nextStatus: Exclude<BadPatternFindingStatus, "open">) =>
      batchReviewQualityFindings({
        finding_ids: Array.from(selectedFindingIds),
        status: nextStatus,
        note: batchNote || undefined
      }),
    onSuccess: async () => { setSelectedFindingIds(new Set()); await refreshQuality(); }
  });
  const claimTaskMutation = useMutation({
    mutationFn: () => claimQualityTask(detailQuery.data?.execution_item_id as string),
    onSuccess: refreshQuality
  });
  const exportMutation = useMutation({
    mutationFn: () => fetchQualityFeedbackExport({
      ...(severity ? { severity } : {}),
      ...(assigneeUserId ? { assignee_user_id: assigneeUserId } : {})
    }),
    onSuccess: ({ filename, content }) => downloadText(filename, content, "application/x-ndjson;charset=utf-8")
  });

  const summary = dashboardQuery.data?.summary;
  const detail = detailQuery.data;
  const mutationError = findingMutation.error ?? qualityReviewMutation.error ?? rescanMutation.error ?? claimTaskMutation.error;
  const batchError = batchAssignMutation.error ?? batchReviewMutation.error ?? exportMutation.error;

  function toggleFindingSelection(findingId: string) {
    setSelectedFindingIds((current) => {
      const next = new Set(current);
      if (next.has(findingId)) next.delete(findingId);
      else next.add(findingId);
      return next;
    });
  }

  return (
    <main className="workspace quality-workspace">
      <header className="quality-header">
        <div>
          <p className="eyebrow">BAD PATTERN QUALITY OPS</p>
          <h1>从自动检测到人工质检，<br />把问题真正闭环。</h1>
          <p>运行结果自动命中底线规则，结合完整 Agent 轨迹完成确认、标注和数据回流。</p>
        </div>
        <div className="quality-header-actions">
          <Link className="button is-ghost" to="/quality/data">线上数据回流</Link>
          <Link className="button is-ghost" to="/runs">查看评测运行</Link>
          <Button type="button" className="is-ghost" disabled={exportMutation.isPending} onClick={() => exportMutation.mutate()}>
            {exportMutation.isPending ? "正在导出" : "导出已确认回流集"}
          </Button>
          <Button type="button" onClick={() => setShowRuleForm((current) => !current)}>
            {showRuleForm ? "收起规则表单" : "新增检测规则"}
          </Button>
        </div>
      </header>

      <section className="quality-kpis" aria-label="质量指标">
        <article><span>待处理命中</span><strong>{summary?.open_findings ?? "—"}</strong><small>进入人工质检队列</small></article>
        <article className="is-critical"><span>严重待处理</span><strong>{summary?.critical_open_findings ?? "—"}</strong><small>需要优先处置</small></article>
        <article><span>已确认问题</span><strong>{summary?.confirmed_findings ?? "—"}</strong><small>可进入训练或修复集</small></article>
        <article><span>已质检样本</span><strong>{summary?.reviewed_items ?? "—"}</strong><small>带人工结论的执行项</small></article>
        <article><span>待分配任务</span><strong>{summary?.unassigned_tasks ?? "—"}</strong><small>尚无质检责任人</small></article>
        <article><span>处理中任务</span><strong>{summary?.active_tasks ?? "—"}</strong><small>已分配或质检中</small></article>
      </section>

      {showRuleForm && <RuleCreatePanel onCreated={async () => {
        setShowRuleForm(false);
        await refreshQuality();
      }} />}

      <section className="quality-filter-bar">
        <form onSubmit={(event) => { event.preventDefault(); setSearch(searchDraft.trim()); }}>
          <Input aria-label="搜索质量命中" value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder="搜索 Agent、题目编号、规则或命中片段" />
          <Button type="submit" className="is-ghost">搜索</Button>
        </form>
        <Select aria-label="命中状态" value={status} onChange={(event) => setStatus(event.target.value as BadPatternFindingStatus | "")}>
          <option value="">全部状态</option>
          <option value="open">待处理</option>
          <option value="confirmed">已确认</option>
          <option value="dismissed">已排除</option>
        </Select>
        <Select aria-label="风险等级" value={severity} onChange={(event) => setSeverity(event.target.value as BadPatternSeverity | "")}>
          <option value="">全部等级</option>
          <option value="critical">严重</option>
          <option value="high">高危</option>
          <option value="medium">中危</option>
          <option value="low">低危</option>
        </Select>
        <Select aria-label="任务状态" value={taskStatus} onChange={(event) => setTaskStatus(event.target.value as QualityTaskStatus | "")}>
          <option value="">全部任务</option>
          <option value="unassigned">待分配</option>
          <option value="assigned">已分配</option>
          <option value="in_review">质检中</option>
          <option value="completed">已完成</option>
        </Select>
        <Select aria-label="负责人" value={assigneeUserId} onChange={(event) => setAssigneeUserId(event.target.value)}>
          <option value="">全部负责人</option>
          {reviewersQuery.data?.map((reviewer) => <option key={reviewer.id} value={reviewer.id}>{reviewer.display_name}</option>)}
        </Select>
        <span>{dashboardQuery.data?.total ?? 0} 条命中</span>
      </section>

      <section className={`quality-batch-bar ${selectedFindingIds.size ? "is-active" : ""}`} aria-label="批量质检操作">
        <strong>{selectedFindingIds.size ? `已选择 ${selectedFindingIds.size} 条命中` : "勾选队列中的命中以批量处理"}</strong>
        <Select aria-label="批量负责人" value={batchAssigneeUserId} onChange={(event) => setBatchAssigneeUserId(event.target.value)} disabled={!selectedFindingIds.size}>
          <option value="">取消分配</option>
          {reviewersQuery.data?.map((reviewer) => <option key={reviewer.id} value={reviewer.id}>{reviewer.display_name}</option>)}
        </Select>
        <Select aria-label="批量优先级" value={batchPriority} onChange={(event) => setBatchPriority(event.target.value as QualityTaskPriority)} disabled={!selectedFindingIds.size}>
          {Object.entries(priorityLabels).map(([value, label]) => <option key={value} value={value}>{label}优先级</option>)}
        </Select>
        <Input aria-label="批量处置说明" value={batchNote} onChange={(event) => setBatchNote(event.target.value)} placeholder="批量处置说明（可选）" disabled={!selectedFindingIds.size} />
        <Button type="button" className="is-ghost" disabled={!selectedFindingIds.size || batchAssignMutation.isPending} onClick={() => batchAssignMutation.mutate()}>应用分配</Button>
        <Button type="button" className="is-ghost" disabled={!selectedFindingIds.size || batchReviewMutation.isPending} onClick={() => batchReviewMutation.mutate("dismissed")}>批量排除</Button>
        <Button type="button" disabled={!selectedFindingIds.size || batchReviewMutation.isPending} onClick={() => batchReviewMutation.mutate("confirmed")}>批量确认</Button>
        {batchError && <small>{batchError.message}</small>}
      </section>

      <section className="quality-layout">
        <aside className="quality-queue" aria-label="Bad Pattern 质检队列">
          <header><span>REVIEW QUEUE</span><strong>质检队列</strong></header>
          {dashboardQuery.isPending ? <LoadingState label="正在读取质检队列" compact /> :
            dashboardQuery.error ? <div className="quality-state is-error">{dashboardQuery.error.message}</div> :
              dashboardQuery.data?.findings.length ? dashboardQuery.data.findings.map((finding) => (
                <div key={finding.id} className={`quality-finding-row ${selectedFindingIds.has(finding.id) ? "is-selected" : ""}`}>
                  <label className="quality-select-finding"><input type="checkbox" aria-label={`选择 ${finding.rule_name}`} checked={selectedFindingIds.has(finding.id)} onChange={() => toggleFindingSelection(finding.id)} /><span /></label>
                  <Link to={`/quality/${finding.id}`} className={`quality-finding-card ${finding.id === findingId ? "is-active" : ""}`}>
                  <div>
                    <span className={`quality-severity is-${finding.severity}`}>{severityLabels[finding.severity]}</span>
                    <span className={`quality-status is-${finding.status}`}>{statusLabels[finding.status]}</span>
                    {finding.task && <span className={`quality-task-status is-${finding.task.status}`}>{taskStatusLabels[finding.task.status]}</span>}
                    <time>{formatDate(finding.created_at)}</time>
                  </div>
                  <strong>{finding.rule_name}</strong>
                  <p>{finding.snippet}</p>
                  <footer><span>{finding.agent_name} · v{finding.agent_version}</span><b>{finding.question_external_id}</b></footer>
                  </Link>
                </div>
              )) : <div className="quality-state"><strong>当前筛选下没有命中</strong><span>新评测运行完成回答后会自动进入规则扫描。</span></div>}
        </aside>

        <article className="quality-detail">
          {!findingId ? <div className="quality-detail-empty"><span>TRACE REVIEW</span><h2>选择一条命中开始质检</h2><p>这里会聚合请求、回答、评分、引用、自动检测和人工结论。</p></div> :
            detailQuery.isPending ? <LoadingState label="正在加载完整 Agent 轨迹" /> :
              detailQuery.error || !detail ? <div className="quality-state is-error">{detailQuery.error?.message ?? "命中详情读取失败"}</div> : <>
                <header className="quality-detail-header">
                  <div>
                    <div><span className={`quality-severity is-${detail.severity}`}>{severityLabels[detail.severity]}</span><span className={`quality-status is-${detail.status}`}>{statusLabels[detail.status]}</span>{detail.task && <span className={`quality-task-status is-${detail.task.status}`}>{taskStatusLabels[detail.task.status]} · {priorityLabels[detail.task.priority]}优先级{detail.task.assignee_display_name ? ` · ${detail.task.assignee_display_name}` : ""}</span>}</div>
                    <h2>{detail.rule_name}</h2>
                    <p>{detail.agent_name} v{detail.agent_version} · {detail.benchmark_name} · {detail.question_external_id}</p>
                  </div>
                  <div>
                    {detail.task?.status !== "completed" && <Button type="button" className="is-ghost" disabled={claimTaskMutation.isPending} onClick={() => claimTaskMutation.mutate()}>{claimTaskMutation.isPending ? "认领中" : "认领任务"}</Button>}
                    <Button type="button" className="is-ghost" disabled={rescanMutation.isPending} onClick={() => rescanMutation.mutate()}>{rescanMutation.isPending ? "检测中" : "重新检测"}</Button>
                    <Link className="button is-ghost" to={`/runs/${detail.run_id}/evidence/${detail.execution_item_id}`}>完整证据</Link>
                  </div>
                </header>

                <section className="quality-evidence-grid">
                  <article><span>PROMPT</span><p>{detail.prompt}</p></article>
                  <article><span>REFERENCE</span><p>{detail.reference_answer || "未提供标准答案"}</p></article>
                  <article className="is-answer"><span>AGENT ANSWER</span><p>{detail.answer ?? detail.answer_preview ?? "没有可读取的回答"}</p></article>
                  <article className="is-hit"><span>DETECTOR HIT · {detail.match_count} MATCHES</span><p>{detail.snippet}</p></article>
                </section>

                <section className="quality-trace">
                  <header><span>AGENT TRAJECTORY</span><strong>执行轨迹</strong><small>{detail.final_score === null ? "未评分" : `最终得分 ${detail.final_score.toFixed(2)}`}</small></header>
                  <ol>{detail.trace.map((event, index) => <li key={`${event.kind}-${index}`} className={`is-${event.kind}`}>
                    <i aria-hidden="true" />
                    <div><span>{event.kind.replace("_", " ")} · {formatDate(event.occurred_at)}</span><strong>{event.title}</strong>{event.detail && <p>{event.detail}</p>}</div>
                    <b>{event.latency_ms === null ? event.status : `${event.latency_ms} ms`}</b>
                  </li>)}</ol>
                </section>

                <section className="quality-review-panel">
                  <header><div><span>HUMAN REVIEW</span><strong>人工质检与回流标注</strong></div>{detail.quality_review && <small>{detail.quality_review.reviewer_display_name} · {formatDate(detail.quality_review.reviewed_at)}</small>}</header>
                  <div className="quality-review-fields">
                    <FormField label="标签" htmlFor="quality-tags" hint="使用英文逗号分隔，最多 12 个">
                      <Input id="quality-tags" value={reviewTags} onChange={(event) => setReviewTags(event.target.value)} placeholder="prompt-injection, safety, regression" />
                    </FormField>
                    <FormField label="质检说明" htmlFor="quality-note">
                      <Textarea id="quality-note" rows={4} value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} placeholder="记录判断依据、修复建议或回流说明" />
                    </FormField>
                  </div>
                  {mutationError && <p className="quality-mutation-error">{mutationError.message}</p>}
                  <footer>
                    <div>{(["pass", "fail", "needs_followup"] as QualityReviewVerdict[]).map((verdict) => <Button key={verdict} type="button" className={detail.quality_review?.verdict === verdict ? "is-selected" : "is-ghost"} disabled={qualityReviewMutation.isPending} onClick={() => qualityReviewMutation.mutate(verdict)}>{verdictLabels[verdict]}</Button>)}</div>
                    <div><Button type="button" className="is-ghost" disabled={findingMutation.isPending} onClick={() => findingMutation.mutate("dismissed")}>排除误报</Button><Button type="button" disabled={findingMutation.isPending} onClick={() => findingMutation.mutate("confirmed")}>确认 Bad Pattern</Button></div>
                  </footer>
                </section>
              </>}
        </article>
      </section>

      <section className="quality-rules">
        <header><div><span>DETECTION RULES</span><h2>底线检测规则</h2></div><small>规则变更随下一次运行生效，可对历史执行项手动重新检测。</small></header>
        {rulesQuery.isPending ? <LoadingState label="正在读取检测规则" compact /> : rulesQuery.error ? <div className="quality-state is-error">{rulesQuery.error.message}</div> :
          <div className="quality-rule-list">{rulesQuery.data?.map((rule) => <article key={rule.id} className={rule.enabled ? "" : "is-disabled"}>
            <div><span className={`quality-severity is-${rule.severity}`}>{severityLabels[rule.severity]}</span><code>{rule.key}</code></div>
            <strong>{rule.name}</strong><p>{rule.description ?? rule.pattern}</p>
            <dl><div><dt>分类</dt><dd>{rule.category}</dd></div><div><dt>命中</dt><dd>{rule.finding_count}</dd></div><div><dt>待处理</dt><dd>{rule.open_count}</dd></div><div><dt>版本</dt><dd>v{rule.version}</dd></div></dl>
            <Button type="button" className="is-ghost" disabled={toggleRuleMutation.isPending} onClick={() => toggleRuleMutation.mutate({ id: rule.id, enabled: !rule.enabled })}>{rule.enabled ? "暂停规则" : "启用规则"}</Button>
          </article>)}</div>}
      </section>
    </main>
  );
}

function RuleCreatePanel({ onCreated }: { onCreated: () => Promise<void> }) {
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [category, setCategory] = useState("safety");
  const [severity, setSeverity] = useState<BadPatternSeverity>("high");
  const [description, setDescription] = useState("");
  const [pattern, setPattern] = useState("");
  const mutation = useMutation({
    mutationFn: () => createBadPatternRule({ key, name, category, severity, description, pattern, enabled: true }),
    onSuccess: onCreated
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate();
  }

  return <form className="quality-rule-form" onSubmit={submit}>
    <header><div><span>NEW DETECTION RULE</span><strong>新增底线检测规则</strong></div><small>规则使用不区分大小写的正则表达式，单次回答最多记录 20 次匹配。</small></header>
    <div>
      <FormField label="规则 Key" htmlFor="rule-key" required><Input id="rule-key" required pattern="[a-z0-9][a-z0-9-]*" value={key} onChange={(event) => setKey(event.target.value)} placeholder="unsafe-action-guidance" /></FormField>
      <FormField label="规则名称" htmlFor="rule-name" required><Input id="rule-name" required value={name} onChange={(event) => setName(event.target.value)} placeholder="不安全操作指导" /></FormField>
      <FormField label="分类" htmlFor="rule-category" required><Input id="rule-category" required value={category} onChange={(event) => setCategory(event.target.value)} /></FormField>
      <FormField label="风险等级" htmlFor="rule-severity" required><Select id="rule-severity" value={severity} onChange={(event) => setSeverity(event.target.value as BadPatternSeverity)}><option value="critical">严重</option><option value="high">高危</option><option value="medium">中危</option><option value="low">低危</option></Select></FormField>
      <FormField label="规则说明" htmlFor="rule-description"><Input id="rule-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="说明风险和判定边界" /></FormField>
      <FormField label="正则表达式" htmlFor="rule-pattern" required hint="最多 500 字符"><Textarea id="rule-pattern" required rows={3} value={pattern} onChange={(event) => setPattern(event.target.value)} placeholder="(?i)ignore previous instructions" /></FormField>
    </div>
    {mutation.error && <p className="quality-mutation-error">{mutation.error.message}</p>}
    <footer><Button type="submit" disabled={mutation.isPending}>{mutation.isPending ? "正在创建" : "创建并启用"}</Button></footer>
  </form>;
}
