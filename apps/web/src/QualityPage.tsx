import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  BadPatternFindingStatus,
  BadPatternSeverity,
  CalibrationQuestionType,
  CalibrationTaskItem,
  JudgeCalibrationAgreement,
  QualityReviewVerdict,
  QualityTaskPriority,
  QualityTaskStatus,
  batchAssignQualityTasks,
  batchReviewQualityFindings,
  claimQualityTask,
  createBadPatternRule,
  fetchBadPatternRules,
  fetchCalibrationReport,
  fetchCalibrationTasks,
  fetchFailureClusters,
  fetchQualityFeedbackExport,
  fetchQualityDashboard,
  fetchQualityFinding,
  fetchQualityReviewers,
  labelCalibrationTask,
  rescanQualityExecutionItem,
  reviewQualityFinding,
  sampleCalibrationTasks,
  updateBadPatternRule,
  upsertQualityReview
} from "./api";
import {
  Button,
  CountUp,
  EmptyState,
  FormField,
  Input,
  LoadingState,
  PageHeader,
  Select,
  StatusPill,
  StatusTone,
  Table,
  Textarea
} from "./components";
import {
  CALIBRATION_JUDGE_PASS_THRESHOLD,
  CALIBRATION_MIN_RELIABLE_SAMPLE,
  formatCalibrationKappa,
  formatCalibrationRate,
  metricDefinitionFor
} from "./metrics";
import { downloadText } from "./userProduct";
import "./quality.css";

const severityLabels: Record<BadPatternSeverity, string> = {
  critical: "严重",
  high: "高危",
  medium: "中危",
  low: "低危"
};

const severityTones: Record<BadPatternSeverity, StatusTone> = {
  critical: "danger",
  high: "warning",
  medium: "info",
  low: "neutral"
};

const statusLabels: Record<BadPatternFindingStatus, string> = {
  open: "待处理",
  confirmed: "已确认",
  dismissed: "已排除"
};

const statusTones: Record<BadPatternFindingStatus, StatusTone> = {
  open: "warning",
  confirmed: "danger",
  dismissed: "neutral"
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

const taskStatusTones: Record<QualityTaskStatus, StatusTone> = {
  unassigned: "neutral",
  assigned: "info",
  in_review: "info",
  completed: "success"
};

const priorityLabels: Record<QualityTaskPriority, string> = {
  low: "低",
  normal: "普通",
  high: "高",
  urgent: "紧急"
};

const calibrationQuestionTypeLabels: Record<CalibrationQuestionType, string> = {
  multiple_choice: "选择题",
  short_answer: "简答题",
  open_ended: "开放题"
};

function calibrationJudgeReason(task: CalibrationTaskItem): string | null {
  const reason = task.judge_details?.judge_reason;
  return typeof reason === "string" && reason ? reason : null;
}

function formatDate(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function kpiValue(value: number | undefined) {
  return value === undefined ? "—" : <CountUp value={value} />;
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
      <PageHeader
        title="从自动检测到人工质检，把问题真正闭环。"
        meta={<>
          <span>{dashboardQuery.data?.total ?? 0} 条命中</span>
          <span>{summary?.open_findings ?? 0} 待处理</span>
          <span>运行结果自动命中底线规则，结合完整 Agent 轨迹完成确认、标注和数据回流。</span>
        </>}
        actions={<>
          <Link className="button is-ghost" to="/quality/data">线上数据回流</Link>
          <Link className="button is-ghost" to="/runs">查看评测运行</Link>
          <Button type="button" className="is-ghost" disabled={exportMutation.isPending} onClick={() => exportMutation.mutate()}>
            {exportMutation.isPending ? "正在导出" : "导出已确认回流集"}
          </Button>
          <Button type="button" onClick={() => setShowRuleForm((current) => !current)}>
            {showRuleForm ? "收起规则表单" : "新增检测规则"}
          </Button>
        </>}
      />

      <section className="quality-kpis stagger-in" aria-label="质量指标">
        <article className="kpi-card"><span>待处理命中</span><strong>{kpiValue(summary?.open_findings)}</strong><small>进入人工质检队列</small></article>
        <article className="kpi-card is-critical"><span>严重待处理</span><strong>{kpiValue(summary?.critical_open_findings)}</strong><small>需要优先处置</small></article>
        <article className="kpi-card"><span>已确认问题</span><strong>{kpiValue(summary?.confirmed_findings)}</strong><small>可进入训练或修复集</small></article>
        <article className="kpi-card"><span>待分配任务</span><strong>{kpiValue(summary?.unassigned_tasks)}</strong><small>处理中 {summary?.active_tasks ?? "—"} · 已质检 {summary?.reviewed_items ?? "—"}</small></article>
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

      <section className="quality-queue" aria-label="Bad Pattern 质检队列">
        <header><h2>质检队列</h2><small>{dashboardQuery.data?.total ?? 0} 条命中</small></header>
        {dashboardQuery.isPending ? <LoadingState label="正在读取质检队列" compact /> :
          dashboardQuery.error ? <div className="quality-state is-error">{dashboardQuery.error.message}</div> :
            dashboardQuery.data?.findings.length ?
              <Table className="quality-table">
                <thead>
                  <tr>
                    <th aria-label="批量选择" />
                    <th>命中规则</th>
                    <th>严重度</th>
                    <th>状态</th>
                    <th data-numeric>匹配</th>
                    <th>样本 / Agent</th>
                    <th>时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {dashboardQuery.data.findings.map((finding) => (
                    <tr key={finding.id} className={`${selectedFindingIds.has(finding.id) ? "is-selected" : ""} ${finding.id === findingId ? "is-active" : ""}`.trim()}>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`选择 ${finding.rule_name}`}
                          checked={selectedFindingIds.has(finding.id)}
                          onChange={() => toggleFindingSelection(finding.id)}
                        />
                      </td>
                      <td><strong>{finding.rule_name}</strong><small>{finding.snippet}</small></td>
                      <td><StatusPill tone={severityTones[finding.severity]}>{severityLabels[finding.severity]}</StatusPill></td>
                      <td>
                        <span className="quality-cell-pills">
                          <StatusPill tone={statusTones[finding.status]}>{statusLabels[finding.status]}</StatusPill>
                          {finding.task && <StatusPill tone={taskStatusTones[finding.task.status]}>{taskStatusLabels[finding.task.status]}</StatusPill>}
                        </span>
                      </td>
                      <td data-numeric>{finding.match_count}</td>
                      <td><span className="quality-cell-meta">{finding.agent_name} · v{finding.agent_version}</span><small>{finding.question_external_id}</small></td>
                      <td><time>{formatDate(finding.created_at)}</time></td>
                      <td><Link className="button is-ghost quality-row-action" to={`/quality/${finding.id}`}>质检</Link></td>
                    </tr>
                  ))}
                </tbody>
              </Table> :
              <EmptyState title="当前筛选下没有命中" description="新评测运行完成回答后会自动进入规则扫描。" />}
      </section>

      <article className="quality-detail">
        {!findingId ? <EmptyState className="quality-detail-empty" title="选择一条命中开始质检" description="这里会聚合请求、回答、评分、引用、自动检测和人工结论。" /> :
          detailQuery.isPending ? <LoadingState label="正在加载完整 Agent 轨迹" /> :
            detailQuery.error || !detail ? <div className="quality-state is-error">{detailQuery.error?.message ?? "命中详情读取失败"}</div> : <>
              <header className="quality-detail-header">
                <div>
                  <div className="quality-detail-pills">
                    <StatusPill tone={severityTones[detail.severity]}>{severityLabels[detail.severity]}</StatusPill>
                    <StatusPill tone={statusTones[detail.status]}>{statusLabels[detail.status]}</StatusPill>
                    {detail.task && <StatusPill tone={taskStatusTones[detail.task.status]}>{taskStatusLabels[detail.task.status]} · {priorityLabels[detail.task.priority]}优先级{detail.task.assignee_display_name ? ` · ${detail.task.assignee_display_name}` : ""}</StatusPill>}
                  </div>
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
                <article><span>PROMPT</span><p>{detail.prompt ?? "held-out 私有题，原文已遮蔽"}</p></article>
                <article><span>REFERENCE</span><p>{detail.reference_answer ?? (detail.held_out ? "held-out 私有题，参考答案已遮蔽" : "未提供标准答案")}</p></article>
                <article className="is-answer"><span>AGENT ANSWER</span><p>{detail.answer ?? detail.answer_preview ?? "没有可读取的回答"}</p></article>
                <article className="is-hit"><span>DETECTOR HIT · {detail.match_count} MATCHES</span><p>{detail.snippet}</p></article>
              </section>

              <section className="quality-trace">
                <header><strong>执行轨迹</strong><small>{detail.final_score === null ? "未评分" : `最终得分 ${detail.final_score.toFixed(2)}`}</small></header>
                <ol>{detail.trace.map((event, index) => <li key={`${event.kind}-${index}`} className={`is-${event.kind}`}>
                  <i aria-hidden="true" />
                  <div><span>{event.kind.replace("_", " ")} · {formatDate(event.occurred_at)}</span><strong>{event.title}</strong>{event.detail && <p>{event.detail}</p>}</div>
                  <b>{event.latency_ms === null ? event.status : `${event.latency_ms} ms`}</b>
                </li>)}</ol>
              </section>

              <section className="quality-review-panel">
                <header><strong>人工质检与回流标注</strong>{detail.quality_review && <small>{detail.quality_review.reviewer_display_name} · {formatDate(detail.quality_review.reviewed_at)}</small>}</header>
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

      <CalibrationPanel />

      <FailureClusterPanel />

      <section className="quality-rules">
        <header><h2>底线检测规则</h2><small>规则变更随下一次运行生效，可对历史执行项手动重新检测。</small></header>
        {rulesQuery.isPending ? <LoadingState label="正在读取检测规则" compact /> : rulesQuery.error ? <div className="quality-state is-error">{rulesQuery.error.message}</div> :
          <div className="quality-rule-list">{rulesQuery.data?.map((rule) => <article key={rule.id} className={rule.enabled ? "" : "is-disabled"}>
            <div><StatusPill tone={severityTones[rule.severity]}>{severityLabels[rule.severity]}</StatusPill><code>{rule.key}</code></div>
            <strong>{rule.name}</strong><p>{rule.description ?? rule.pattern}</p>
            <dl><div><dt>分类</dt><dd>{rule.category}</dd></div><div><dt>命中</dt><dd>{rule.finding_count}</dd></div><div><dt>待处理</dt><dd>{rule.open_count}</dd></div><div><dt>版本</dt><dd>v{rule.version}</dd></div></dl>
            <Button type="button" className="is-ghost" disabled={toggleRuleMutation.isPending} onClick={() => toggleRuleMutation.mutate({ id: rule.id, enabled: !rule.enabled })}>{rule.enabled ? "暂停规则" : "启用规则"}</Button>
          </article>)}</div>}
      </section>
    </main>
  );
}

function CalibrationPanel() {
  const queryClient = useQueryClient();
  const reportQuery = useQuery({ queryKey: ["quality-calibration-report"], queryFn: fetchCalibrationReport });
  const tasksQuery = useQuery({
    queryKey: ["quality-calibration-tasks"],
    queryFn: () => fetchCalibrationTasks("pending")
  });

  async function refreshCalibration() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["quality-calibration-report"] }),
      queryClient.invalidateQueries({ queryKey: ["quality-calibration-tasks"] })
    ]);
  }

  const sampleMutation = useMutation({
    mutationFn: () => sampleCalibrationTasks(),
    onSuccess: refreshCalibration
  });
  const labelMutation = useMutation({
    mutationFn: ({ taskId, label }: { taskId: string; label: JudgeCalibrationAgreement }) =>
      labelCalibrationTask(taskId, { label }),
    onSuccess: refreshCalibration
  });

  const summary = reportQuery.data?.overall;
  const missingSample = summary ? Math.max(CALIBRATION_MIN_RELIABLE_SAMPLE - summary.labeled, 0) : 0;
  const mutationError = sampleMutation.error ?? labelMutation.error;

  return (
    <section className="quality-calibration" aria-label="裁判校准">
      <header>
        <h2>裁判校准</h2>
        <small>随机抽取已评分样本，人工核对裁判判定是否准确，用 Cohen's Kappa 衡量裁判与人工的一致性。</small>
      </header>
      {reportQuery.isPending ? <LoadingState label="正在读取校准报告" compact /> :
        reportQuery.error ? <div className="quality-state is-error">{reportQuery.error.message}</div> :
          summary && <>
            <div className="quality-calibration-report">
              <dl>
                <div><dt>Kappa 系数</dt><dd>{formatCalibrationKappa(summary.kappa)}</dd></div>
                <div><dt>一致率</dt><dd>{formatCalibrationRate(summary.agreement)}</dd></div>
                <div><dt>已标注样本</dt><dd>{summary.labeled} / {CALIBRATION_MIN_RELIABLE_SAMPLE}</dd></div>
                <div><dt>同意 / 不同意</dt><dd>{summary.agree} / {summary.disagree}</dd></div>
              </dl>
              {summary.insufficient_sample ?
                <p className="is-warning">样本量不足，至少还需标注 {missingSample} 条，Kappa 与一致率才具备统计参考价值。</p> :
                <p>样本量已达标，Kappa 越接近 1 代表裁判与人工判定越一致。</p>}
              <Button type="button" disabled={sampleMutation.isPending} onClick={() => sampleMutation.mutate()}>
                {sampleMutation.isPending ? "正在抽样" : "抽取样本"}
              </Button>
            </div>
            {sampleMutation.data &&
              <p className="quality-calibration-sample">本次抽样候选 {sampleMutation.data.eligible} 条，新增标注任务 {sampleMutation.data.created} 条。</p>}
          </>}
      {mutationError && <p className="quality-mutation-error">{mutationError.message}</p>}
      <div className="quality-calibration-list">
        {tasksQuery.isPending ? <LoadingState label="正在读取待标注任务" compact /> :
          tasksQuery.error ? <div className="quality-state is-error">{tasksQuery.error.message}</div> :
            tasksQuery.data?.items.length ? tasksQuery.data.items.map((task) => {
              const passed = task.judge_score >= CALIBRATION_JUDGE_PASS_THRESHOLD;
              const reason = calibrationJudgeReason(task);
              return (
                <article key={task.id}>
                  <header>
                    <StatusPill tone={passed ? "success" : "danger"}>
                      {passed ? "裁判判定通过" : "裁判判定未通过"} · {task.judge_score.toFixed(0)} 分
                    </StatusPill>
                    <strong>{task.question_external_id}</strong>
                    <time>{formatDate(task.created_at)}</time>
                  </header>
                  <p>{task.agent_name} · {task.run_name} · {metricDefinitionFor(task.ability)?.label ?? task.ability} · {calibrationQuestionTypeLabels[task.question_type]}</p>
                  <p>裁判理由：{reason ?? "未记录判定理由"}</p>
                  <footer>
                    <Link className="button is-ghost" to={`/runs/${task.run_id}/evidence/${task.execution_item_id}`}>完整证据</Link>
                    <span>你是否同意裁判的判定？</span>
                    <Button type="button" className="is-ghost" disabled={labelMutation.isPending}
                      onClick={() => labelMutation.mutate({ taskId: task.id, label: "disagree" })}>不同意</Button>
                    <Button type="button" disabled={labelMutation.isPending}
                      onClick={() => labelMutation.mutate({ taskId: task.id, label: "agree" })}>同意</Button>
                  </footer>
                </article>
              );
            }) : <EmptyState title="暂无待标注的校准任务" description="点击「抽取样本」从最近的评测运行中随机抽取已评分样本。" />}
      </div>
    </section>
  );
}

function FailureClusterPanel() {
  const clustersQuery = useQuery({
    queryKey: ["quality-failure-clusters"],
    queryFn: () => fetchFailureClusters()
  });

  return (
    <section className="quality-clusters" aria-label="失败聚类">
      <header>
        <h2>失败聚类</h2>
        <small>评测运行完成后，自动对失败与低分（&lt;60）回答做两阶段 LLM 聚类，归并成命名问题组辅助人工质检。</small>
      </header>
      {clustersQuery.isPending ? <LoadingState label="正在读取失败聚类" compact /> :
        clustersQuery.error ? <div className="quality-state is-error">{clustersQuery.error.message}</div> :
          clustersQuery.data?.length ?
            <div className="quality-cluster-list stagger-in">
              {clustersQuery.data.map((cluster) => (
                <article key={cluster.id}>
                  <header>
                    <strong>{cluster.name}</strong>
                    <StatusPill tone="info">{cluster.sample_count} 个样本</StatusPill>
                    <time>{formatDate(cluster.created_at)}</time>
                  </header>
                  <p>{cluster.description ?? "未提供簇描述"}</p>
                  <p className="quality-cluster-scope">
                    {cluster.run_name ? `运行：${cluster.run_name}` : `范围：最近 ${cluster.window_days} 天`} · 共分析 {cluster.failure_count} 条失败/低分回答
                  </p>
                  {cluster.samples.length > 0 && <footer>
                    <span>代表样本</span>
                    {cluster.samples.map((sample) => (
                      <Link key={sample.execution_item_id} className="button is-ghost"
                        to={`/runs/${sample.run_id}/evidence/${sample.execution_item_id}`}>
                        证据 {sample.execution_item_id.slice(0, 8)}
                      </Link>
                    ))}
                  </footer>}
                </article>
              ))}
            </div> :
            <EmptyState
              title="暂无失败聚类"
              description="评测运行完成且失败/低分回答达到 5 条时会自动聚类，也可通过 maintenance 的 cluster-failures 命令手动补跑。"
            />}
    </section>
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
    <header><strong>新增底线检测规则</strong><small>规则使用不区分大小写的正则表达式，单次回答最多记录 20 次匹配。</small></header>
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
