import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  Ability,
  ApiError,
  RunAgentOption,
  RunListItem,
  RunProfileCompatibility,
  RunProgress,
  RunStage,
  RunStatus,
  cancelRun,
  createRunsBatch,
  fetchRunOptions,
  fetchRunProgress,
  fetchRuns,
  retryRunScoring
} from "./api";
import { Button, FormField, Input, LoadingState, Modal, Table } from "./components";
import { abilityMetrics, leaderboardMetrics } from "./metrics";

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
  system_failed: "系统失败",
  canceled: "已取消"
};

const stageLabels: Record<RunStage, string> = {
  preparing: "准备执行矩阵",
  inference: "Agent 推理",
  citation_fetch: "引用抓取",
  judging: "模型裁判",
  aggregating: "分数汇总",
  ready_to_publish: "发布完成",
  publishing: "正在发布"
};

const abilityLabels = Object.fromEntries(abilityMetrics.map((metric) => [metric.key, metric.shortLabel])) as Record<Ability, string>;

const failureLabels: Record<string, string> = {
  agent_authentication: "鉴权失败",
  agent_input_limit: "输入超限",
  agent_client_error: "请求参数",
  agent_response_schema: "响应结构",
  agent_timeout: "请求超时",
  agent_network: "网络异常",
  agent_rate_limit: "接口限流",
  agent_server_error: "上游异常",
  agent_response_too_large: "响应过大",
  agent_circuit_authentication: "鉴权熔断",
  agent_circuit_input_limit: "超限跳过",
  agent_circuit_client_error: "参数熔断",
  agent_circuit_response_schema: "结构熔断"
};

const validityReasonLabels: Record<string, string> = {
  authentication_failure: "Agent 鉴权配置失败",
  configuration_failure_rate_at_least_20_percent: "配置类请求失败达到 20%",
  no_execution_items: "没有可评分的执行项"
};

function publicationLabel(destinations: string[]) {
  return destinations.length > 0 ? destinations.join(" / ") : "不发布";
}

function profileCompatibility(
  agent: RunAgentOption,
  profileKey: string
): RunProfileCompatibility {
  return agent.profile_compatibility?.find((item) => item.profile_key === profileKey) ?? {
    profile_key: profileKey,
    compatible: true,
    oversized_question_count: 0,
    max_question_characters: 0,
    citation_required_count: 0,
    blocking_reasons: [],
    warnings: []
  };
}

function formatDate(value: string | null) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function formatCount(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : new Intl.NumberFormat("zh-CN").format(value);
}

function pollInterval(status?: RunStatus) {
  if (status && terminalStatuses.has(status)) return false;
  return document.visibilityState === "visible" ? 3_000 : 15_000;
}

function statusClass(status: RunStatus) {
  if (status === "published" || status === "completed") return "is-success";
  if (status === "system_failed" || status === "canceled") return "is-danger";
  if (status === "running" || status === "scoring") return "is-active";
  return "";
}

function runProfileLabel(run: Pick<RunListItem, "run_scope" | "target_ability">) {
  if (run.run_scope === "smoke") return "冒烟测试";
  if (run.run_scope === "specialist" && run.target_ability) {
    return `${abilityLabels[run.target_ability]}专项`;
  }
  return "综合评测";
}

function publishedMessage(run: RunProgress) {
  if (!run.ranking_eligible) return "评测完成，结果已保存，本次不更新排行榜。";
  if (run.run_scope === "specialist") return "评测完成，专项成绩已汇入对应能力榜。";
  return "评测完成，综合成绩和五项能力成绩已汇入排行榜。";
}

function LaunchRunModal({
  initialAgentVersion,
  initialProfileKey,
  onClose
}: {
  initialAgentVersion?: string;
  initialProfileKey?: string;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const optionsQuery = useQuery({ queryKey: ["run-options"], queryFn: fetchRunOptions });
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [name, setName] = useState(() => {
    const stamp = new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit"
    }).format(new Date());
    return `Agent 基准评测 ${stamp}`;
  });
  const [selectedProfileKey, setSelectedProfileKey] = useState(initialProfileKey ?? "");
  const [selected, setSelected] = useState<string[]>(
    initialAgentVersion ? [initialAgentVersion] : []
  );
  const [formError, setFormError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async (forceRetest: boolean) => {
      const options = optionsQuery.data;
      if (!options?.judge_config_id) {
        throw new Error("评测配置尚未准备完成");
      }
      const profileKey = selectedProfileKey || options.default_profile_key;
      const profile = options.profiles.find((item) => item.key === profileKey);
      if (!profile) {
        throw new Error("请选择运行 Profile");
      }
      if (!profile.score_config_id) throw new Error("该 Profile 的评分配置尚未准备完成");
      return createRunsBatch({
        name: name.trim(),
        agent_version_ids: selectedAgentIds,
        profile_key: profileKey,
        judge_config_id: options.judge_config_id,
        score_config_id: profile.score_config_id,
        ...(forceRetest ? { force_retest: true } : {})
      });
    },
    onSuccess: async ({ runs }) => {
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
      onClose();
      navigate(runs.length === 1 ? `/runs/${runs[0].id}` : "/runs");
    }
  });

  function toggleAgent(versionId: string) {
    setSelected((current) => {
      const validCurrent = current.filter((value) =>
        optionsQuery.data?.agents.some((agent) => agent.agent_version_id === value)
      );
      if (validCurrent.includes(versionId)) {
        return validCurrent.filter((value) => value !== versionId);
      }
      if (validCurrent.length >= 50) return validCurrent;
      return [...validCurrent, versionId];
    });
    setFormError(null);
    mutation.reset();
  }

  function continueFromAgents(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!name.trim()) return setFormError("请输入任务名称");
    if (selectedAgentIds.length < 1 || selectedAgentIds.length > 50) {
      return setFormError("请选择 1-50 个 Agent");
    }
    setFormError(null);
    setStep(2);
  }

  function continueFromProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedProfile) return setFormError("请选择运行 Profile");
    const incompatible = selectedAgents?.filter(
      (agent) => !profileCompatibility(agent, selectedProfile.key).compatible
    ) ?? [];
    if (incompatible.length) {
      return setFormError(`${incompatible.map((agent) => agent.name).join("、")} 与该 Profile 不兼容`);
    }
    setFormError(null);
    setStep(3);
  }

  const options = optionsQuery.data;
  const selectedAgentIds = selected.filter((versionId) =>
    options?.agents.some((agent) => agent.agent_version_id === versionId)
  );
  const effectiveProfileKey = options?.profiles.some(
    (profile) => profile.key === selectedProfileKey
  )
    ? selectedProfileKey
    : options?.default_profile_key;
  const selectedProfile = options?.profiles.find((profile) => profile.key === effectiveProfileKey);
  const selectedAgents = options?.agents.filter((agent) =>
    selectedAgentIds.includes(agent.agent_version_id)
  );
  const duplicateCode = mutation.error instanceof ApiError ? mutation.error.code : undefined;

  return (
    <Modal
      eyebrow="NEW EVALUATION RUN"
      title="发起基准评测"
      description="选择 1-50 个已启用 Agent 和运行 Profile；系统会为每个 Agent 创建独立任务。"
      onClose={onClose}
    >
      <ol className="stepper run-stepper" aria-label="评测步骤">
        <li className={step === 1 ? "is-current" : "is-done"}><span>01</span>选择 Agent</li>
        <li className={step === 2 ? "is-current" : step === 3 ? "is-done" : ""}><span>02</span>选择 Profile</li>
        <li className={step === 3 ? "is-current" : ""}><span>03</span>确认并提交</li>
      </ol>

      {optionsQuery.isPending ? (
        <LoadingState label="正在读取评测配置" compact />
      ) : optionsQuery.error ? (
        <div className="modal-form"><div className="result-banner is-error">{optionsQuery.error.message}</div></div>
      ) : !options?.ready ? (
        <div className="modal-form"><div className="result-banner is-error">{options?.issues.join("；")}</div></div>
      ) : step === 1 ? (
        <form className="modal-form run-launch-form" onSubmit={continueFromAgents}>
          <FormField label="任务名称" htmlFor="run-name" required>
            <Input id="run-name" value={name} maxLength={160} onChange={(event) => setName(event.target.value)} />
          </FormField>
          <div className="independent-run-note"><strong>独立任务模式</strong><span>每个已选 Agent 会创建一个独立评测任务，可分别完成、取消和发布，互不阻塞。</span></div>
          <fieldset className="agent-picker">
            <legend>Agent 选择 <span>{selectedAgentIds.length}/50 · 至少选择 1 个</span></legend>
            <div>
              {options.agents.map((agent) => {
                const checked = selectedAgentIds.includes(agent.agent_version_id);
                return (
                  <label className={checked ? "is-selected" : ""} key={agent.agent_version_id}>
                    <input type="checkbox" checked={checked} disabled={!checked && selectedAgentIds.length >= 50} onChange={() => toggleAgent(agent.agent_version_id)} />
                    <span><strong>{agent.name}</strong><small>{agent.provider} · V{agent.version}</small>{agent.lifecycle_status === "verified" && <small className="agent-constraint-copy">已验证候选 · 仅 Smoke</small>}</span>
                    <i>{checked ? "已选择" : agent.lifecycle_status === "verified" ? "候选" : "选择"}</i>
                  </label>
                );
              })}
            </div>
          </fieldset>
          {formError && <div className="result-banner is-error" role="alert">{formError}</div>}
          <div className="modal-actions"><Button type="button" className="is-ghost" onClick={onClose}>取消</Button><Button type="submit">下一步：选择 Profile</Button></div>
        </form>
      ) : step === 2 ? (
        <form className="modal-form run-launch-form" onSubmit={continueFromProfile}>
          <fieldset className="profile-picker">
            <legend>运行 Profile <span>{options.profiles.length} 种配置</span></legend>
            <div>
              {options.profiles.map((profile) => {
                const checked = profile.key === effectiveProfileKey;
                const checks = selectedAgents?.map((agent) => ({
                  agent,
                  compatibility: profileCompatibility(agent, profile.key)
                })) ?? [];
                const blocked = checks.filter((item) => !item.compatibility.compatible);
                const scope = profile.scope === "comprehensive"
                  ? "综合评测"
                  : profile.scope === "smoke"
                    ? "冒烟检查"
                    : `${profile.target_ability ? abilityLabels[profile.target_ability] : "专项"}专项`;
                return (
                  <label className={`${checked ? "is-selected" : ""} ${blocked.length ? "is-incompatible" : ""}`} key={profile.key}>
                    <input
                      type="radio"
                      name="run-profile"
                      value={profile.key}
                      aria-label={`${profile.name} Profile`}
                      checked={checked}
                      disabled={blocked.length > 0}
                      onChange={() => {
                        setSelectedProfileKey(profile.key);
                        setFormError(null);
                      }}
                    />
                    <header><span>{scope}</span><strong>{profile.name}</strong><small>{profile.description}</small></header>
                    <dl>
                      <div><dt>题目</dt><dd>{profile.question_count}</dd></div>
                      <div><dt>Benchmark</dt><dd>{profile.benchmark_count}</dd></div>
                    </dl>
                    <footer><span>发布至</span><strong>{publicationLabel(profile.publishes_to)}</strong></footer>
                    {!profile.ranking_eligible && <span className="profile-notice">验证用途，不计入排行榜</span>}
                  </label>
                );
              })}
            </div>
          </fieldset>
          {formError && <div className="result-banner is-error" role="alert">{formError}</div>}
          <div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => setStep(1)}>返回 Agent</Button><Button type="submit">下一步：确认配置</Button></div>
        </form>
      ) : selectedProfile ? (
        <div className="modal-form run-review">
          <div className="review-banner"><span>READY TO QUEUE · INDEPENDENT TASKS</span><strong>{name}</strong><small>以下 {selectedAgentIds.length} 个任务会独立执行、取消、完成和发布。</small></div>
          <dl className="review-metrics">
            <div><dt>独立任务</dt><dd>{selectedAgentIds.length} 个</dd></div>
            <div><dt>每任务题目</dt><dd>{selectedProfile.question_count}</dd></div>
            <div><dt>预计总调用</dt><dd>{selectedProfile.question_count * selectedAgentIds.length}</dd></div>
            <div><dt>运行 Profile</dt><dd>{selectedProfile.name}</dd></div>
          </dl>
          <section className="independent-task-list" aria-label="独立评测任务">
            {selectedAgents?.map((agent, index) => {
              const check = profileCompatibility(agent, selectedProfile.key);
              return <article key={agent.agent_version_id}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{agent.name}</strong><small>{agent.provider} · V{agent.version}</small>{check.warnings.map((warning) => <small className="compatibility-warning" key={warning}>{warning}</small>)}</div><b>{check.compatible ? `${selectedProfile.question_count} 题` : "不兼容"}</b></article>;
            })}
          </section>
          <section className="run-profile-review">
            <header><div><span>PROFILE DATASETS</span><strong>{selectedProfile.name}</strong></div><small>{selectedProfile.benchmark_count} 个 Benchmark · 裁判 {options.judge_model}</small></header>
            <div>{selectedProfile.datasets.map((dataset) => <article key={dataset.dataset_version_id}><div><strong>{dataset.name}</strong><small>{abilityLabels[dataset.ability]} · {dataset.language.toUpperCase()} · V{dataset.version}</small></div><b>{dataset.question_count} 题</b></article>)}</div>
            <footer><span>发布去向</span><strong>{publicationLabel(selectedProfile.publishes_to)}</strong><small>{selectedProfile.ranking_eligible ? "计入排行榜" : "非排名运行，不计入排行榜"}</small></footer>
          </section>
          {!selectedProfile.ranking_eligible && <div className="smoke-review-warning"><strong>Smoke Profile</strong><span>本次结果用于快速验证，不会进入排行榜。</span></div>}
          {duplicateCode === "evaluation_already_active" ? <div className="result-banner is-error" role="alert">所选 Agent 已有相同 Profile 的评测正在排队或运行，不能强制覆盖。请等待现有任务结束后再试。</div> : duplicateCode === "evaluation_result_exists" ? <div className="duplicate-confirm" role="alert"><div><strong>已有可用评测结果</strong><span>重新评测会创建全新独立任务，不会覆盖历史结果。确认后才会强制复测。</span></div><Button type="button" className="is-danger" disabled={mutation.isPending} onClick={() => mutation.mutate(true)}>{mutation.isPending ? "正在重新提交" : "确认重新评测"}</Button></div> : mutation.error ? <div className="result-banner is-error" role="alert">{mutation.error instanceof ApiError ? mutation.error.message : "任务创建失败"}</div> : null}
          <div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => { mutation.reset(); setStep(2); }}>返回修改</Button><Button type="button" disabled={mutation.isPending || Boolean(duplicateCode)} onClick={() => mutation.mutate(false)}>{mutation.isPending ? "正在提交" : `提交 ${selectedAgentIds.length} 个独立任务`}</Button></div>
        </div>
      ) : null}
    </Modal>
  );
}

function RunsList() {
  const [searchParameters, setSearchParameters] = useSearchParams();
  const [quickSelection, setQuickSelection] = useState(() => searchParameters.get("launch") === "1" ? {
    agentVersion: searchParameters.get("agentVersion") ?? undefined,
    profile: searchParameters.get("profile") ?? undefined
  } : null);
  const [launchOpen, setLaunchOpen] = useState(Boolean(quickSelection));
  const runsQuery = useQuery({
    queryKey: ["runs"],
    queryFn: fetchRuns,
    refetchInterval: () => pollInterval()
  });
  const runs = runsQuery.data ?? [];
  const activeCount = runs.filter((run) => !terminalStatuses.has(run.status)).length;
  const publishedCount = runs.filter((run) => run.status === "published").length;

  function openLaunch() {
    setQuickSelection(null);
    setLaunchOpen(true);
  }

  function closeLaunch() {
    setLaunchOpen(false);
    setQuickSelection(null);
    if (["launch", "agentVersion", "profile"].some((key) => searchParameters.has(key))) {
      const next = new URLSearchParams(searchParameters);
      next.delete("launch");
      next.delete("agentVersion");
      next.delete("profile");
      setSearchParameters(next, { replace: true });
    }
  }

  return (
    <main className="workspace runs-workspace">
      <header className="section-header">
        <div><p className="eyebrow">EVALUATION PIPELINE</p><h1>评测任务</h1><p>按运行 Profile 为每个 Agent 创建独立基准测试，分别跟踪推理、裁判、引用和发布进度。</p></div>
        <Button type="button" onClick={openLaunch}>+ 发起评测</Button>
      </header>
      <section className="run-summary-strip">
        <div><span>任务总数</span><strong>{runs.length}</strong></div>
        <div><span>执行中</span><strong>{activeCount}</strong></div>
        <div><span>已发布</span><strong>{publishedCount}</strong></div>
        <p>运行状态每 3 秒同步；页面不可见时自动降低轮询频率。</p>
      </section>
      {runsQuery.isPending ? (
        <LoadingState label="正在读取评测任务" />
      ) : runsQuery.error ? (
        <div className="table-state is-error">{runsQuery.error.message}</div>
      ) : runs.length === 0 ? (
        <section className="empty-state"><span>NO RUNS</span><h2>尚未发起评测</h2><p>选择 1-50 个已启用 Agent 和运行 Profile，创建彼此独立的评测任务。</p><Button type="button" onClick={openLaunch}>发起首个评测</Button></section>
      ) : (
        <Table className="runs-table">
          <thead><tr><th>任务</th><th>状态</th><th>处理进度</th><th>失败 / 重试</th><th>创建时间</th><th><span className="sr-only">查看</span></th></tr></thead>
          <tbody>{runs.map((run: RunListItem) => {
            const processed = run.completed + run.failed;
            const percent = run.total ? Math.round(processed * 100 / run.total) : 0;
            return <tr key={run.id}><td><strong>{run.name}</strong><small>{runProfileLabel(run)} · {run.agent_count} Agent · {run.total} 执行项</small></td><td><span className={`run-status ${statusClass(run.status)}`}><i />{run.desired_state === "canceled" && run.status !== "canceled" ? "取消处理中" : statusLabels[run.status]}</span><small className="stage-copy">{stageLabels[run.stage]}</small></td><td><div className="table-progress"><span><i style={{ width: `${percent}%` }} /></span><small>{processed} / {run.total} · {percent}%</small></div></td><td className="mono">{run.failed} / {run.retried}</td><td className="mono">{formatDate(run.created_at)}</td><td><Link className="row-link" to={`/runs/${run.id}`}>查看详情 →</Link></td></tr>;
          })}</tbody>
        </Table>
      )}
      {launchOpen && <LaunchRunModal initialAgentVersion={quickSelection?.agentVersion} initialProfileKey={quickSelection?.profile} onClose={closeLaunch} />}
    </main>
  );
}

function RunDetail({ runId }: { runId: string }) {
  const queryClient = useQueryClient();
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [confirmRetryScoring, setConfirmRetryScoring] = useState(false);
  const progressQuery = useQuery({
    queryKey: ["run-progress", runId],
    queryFn: () => fetchRunProgress(runId),
    refetchInterval: (query) => pollInterval(query.state.data?.status)
  });
  const cancelMutation = useMutation({
    mutationFn: () => cancelRun(runId),
    onSuccess: async () => {
      setConfirmCancel(false);
      await queryClient.invalidateQueries({ queryKey: ["run-progress", runId] });
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
    }
  });
  const retryScoringMutation = useMutation({
    mutationFn: () => retryRunScoring(runId),
    onSuccess: async () => {
      setConfirmRetryScoring(false);
      await queryClient.invalidateQueries({ queryKey: ["run-progress", runId] });
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
    }
  });
  const run: RunProgress | undefined = progressQuery.data;
  const cancelable = run && !terminalStatuses.has(run.status) && run.desired_state !== "canceled";
  const scoringRetryable = run?.status === "system_failed"
    && run.stage === "judging"
    && Boolean(run.system_error?.startsWith("answer_scoring_"));

  if (progressQuery.isPending) return <main className="workspace"><LoadingState label="正在读取运行状态" /></main>;
  if (progressQuery.error || !run) return <main className="workspace"><div className="table-state is-error">{progressQuery.error?.message ?? "评测任务不存在"}</div></main>;

  return (
    <main className="workspace run-detail-workspace">
      <Link className="back-link" to="/runs">← 返回任务列表</Link>
      <header className="run-detail-header">
        <div><p className="eyebrow">RUN / {run.id.slice(0, 8).toUpperCase()}</p><h1>{run.name}</h1><div className="run-meta"><span className={`run-status ${statusClass(run.status)}`}><i />{run.desired_state === "canceled" && run.status !== "canceled" ? "取消处理中" : statusLabels[run.status]}</span><span>{runProfileLabel(run)}</span><span>{stageLabels[run.stage]}</span><span>创建于 {formatDate(run.created_at)}</span></div></div>
        {cancelable ? <Button type="button" className="is-danger" onClick={() => setConfirmCancel(true)}>取消任务</Button> : scoringRetryable ? <Button type="button" onClick={() => setConfirmRetryScoring(true)}>重试评分</Button> : null}
      </header>

      <section className={`run-terminal ${terminalStatuses.has(run.status) ? "is-visible" : ""} ${statusClass(run.status)}`}>
        <span>{run.status === "published" ? "PUBLISHED" : run.status === "system_failed" ? "SYSTEM FAILURE" : run.status === "canceled" ? "CANCELED" : "IN PROGRESS"}</span>
        <strong>{run.status === "published" ? publishedMessage(run) : run.status === "system_failed" ? "平台处理发生系统异常，旧榜单未受影响。" : run.status === "canceled" ? "任务已取消，未发布任何结果。" : `当前阶段：${stageLabels[run.stage]}`}</strong>
        {run.system_error && <small>{run.system_error}</small>}
      </section>

      <section className="run-progress-panel">
        <header><div><span>OVERALL PROGRESS</span><strong>{run.progress_percent.toFixed(2)}%</strong></div><p>{run.processed} / {run.total} 执行项已处理</p></header>
        <div className="large-progress"><i style={{ width: `${run.progress_percent}%` }} /></div>
        <dl><div><dt>成功</dt><dd>{run.completed}</dd></div><div><dt>失败</dt><dd>{run.failed}</dd></div><div><dt>重试</dt><dd>{run.retried}</dd></div><div><dt>心跳</dt><dd>{formatDate(run.heartbeat_at)}</dd></div></dl>
      </section>

      <section className="agent-progress-grid">
        {run.agents.map((agent, index) => {
          const percent = agent.total ? agent.processed * 100 / agent.total : 0;
          return <article key={agent.run_agent_id}><header><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{agent.agent_name}</strong><small>{agent.provider} · V{agent.version}</small></div></header><div className="agent-progress-value"><strong>{percent.toFixed(0)}%</strong><span>{agent.processed} / {agent.total}</span></div><div className="agent-progress-bar"><i style={{ width: `${percent}%` }} /></div><footer><span>成功 <b>{agent.completed}</b></span><span>失败 <b>{agent.failed}</b></span><span>重试 <b>{agent.retried}</b></span></footer>{agent.validity_status === "invalid" && <div className="result-validity-invalid"><strong>接入配置无效，不进入排行榜</strong><span>{validityReasonLabels[agent.validity_reason ?? ""] ?? agent.validity_reason} · {agent.configuration_failure_count} 项 / {agent.configuration_failure_rate.toFixed(2)}%</span></div>}{agent.result && <section className="run-result-summary six-metric-grid">{leaderboardMetrics.map((metric) => <div key={metric.key}><span>{metric.shortLabel}</span><strong>{metric.key === "overall" ? agent.result?.overall_score.toFixed(2) : agent.result?.ability_scores[metric.key]?.toFixed(2) ?? "—"}</strong></div>)}<footer><span>成功率 {agent.result.success_rate.toFixed(2)}%</span><span>HTTP {agent.result.http_call_count}</span><span>Token {formatCount(agent.result.total_tokens)}</span><span>P50/P95 {agent.result.latency_p50_ms ?? "—"}/{agent.result.latency_p95_ms ?? "—"} ms</span></footer></section>}{Object.keys(agent.failure_categories ?? {}).length > 0 && <div className="failure-category-list">{Object.entries(agent.failure_categories).map(([category, count]) => <span key={category}>{failureLabels[category] ?? category}<b>{count}</b></span>)}</div>}</article>;
        })}
      </section>

      {confirmCancel && <Modal eyebrow="DESTRUCTIVE ACTION" title="确认取消评测？" description="已完成的执行项会保留，但该批次不会发布榜单。" onClose={() => setConfirmCancel(false)}><div className="modal-form cancel-confirm"><p>任务：<strong>{run.name}</strong></p>{cancelMutation.error && <div className="result-banner is-error">{cancelMutation.error.message}</div>}<div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => setConfirmCancel(false)}>继续运行</Button><Button type="button" className="is-danger" disabled={cancelMutation.isPending} onClick={() => cancelMutation.mutate()}>{cancelMutation.isPending ? "正在取消" : "确认取消任务"}</Button></div></div></Modal>}
      {confirmRetryScoring && <Modal eyebrow="RESUME JUDGING" title="继续未完成的评分？" description="已完成的 Agent 推理和评分会保留，只调用 DeepSeek 处理尚未评分的题目。" onClose={() => setConfirmRetryScoring(false)}><div className="modal-form cancel-confirm"><p>任务：<strong>{run.name}</strong></p>{retryScoringMutation.error && <div className="result-banner is-error">{retryScoringMutation.error.message}</div>}<div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => setConfirmRetryScoring(false)}>暂不重试</Button><Button type="button" disabled={retryScoringMutation.isPending} onClick={() => retryScoringMutation.mutate()}>{retryScoringMutation.isPending ? "正在重新排队" : "确认重试评分"}</Button></div></div></Modal>}
    </main>
  );
}

export function RunsPage() {
  const { runId } = useParams();
  return runId ? <RunDetail runId={runId} /> : <RunsList />;
}
