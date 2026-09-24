import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CSSProperties, FormEvent, useRef, useState } from "react";
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
  createEvaluationExperiment,
  createRun,
  fetchRunOptions,
  fetchRunProgress,
  fetchRuns,
  retryRunScoring
} from "./api";
import {
  Button,
  EmptyState,
  FormField,
  Input,
  LoadingState,
  Modal,
  PageHeader,
  ProgressBar,
  Select,
  StatusPill,
  Table
} from "./components";
import type { StatusTone } from "./components";
import "./runs.css";
import { randomId } from "./randomId";
import { abilityMetrics, metricDefinitionFor, formatCostUsd, leaderboardMetrics } from "./metrics";
import { runSharedElementTransition, runWithViewTransition, viewTransitionsSupported } from "./viewTransitions";

const terminalStatuses = new Set<RunStatus>([
  "completed",
  "published",
  "system_failed",
  "canceled"
]);

const activeStatuses = new Set<RunStatus>(["queued", "running", "paused", "scoring"]);

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

function runStatusLabel(run: { status: RunStatus; desired_state: string }) {
  return run.desired_state === "canceled" && run.status !== "canceled"
    ? "取消处理中"
    : statusLabels[run.status];
}

function runStatusTone(run: { status: RunStatus; desired_state: string }): StatusTone {
  if (run.desired_state === "canceled" && run.status !== "canceled") return "warning";
  if (run.status === "running" || run.status === "scoring") return "info";
  if (run.status === "queued" || run.status === "paused") return "warning";
  if (run.status === "completed" || run.status === "published") return "success";
  if (run.status === "system_failed" || run.status === "canceled") return "danger";
  return "neutral";
}

/* 状态图标语义：进行中 = 脉冲点，已发布 = 印章徽章。 */
function runStatusPillClass(run: { status: RunStatus }): string {
  if (run.status === "running" || run.status === "scoring") return "is-pulsing";
  if (run.status === "published") return "is-stamp";
  return "";
}

function terminalClass(status: RunStatus) {
  if (status === "published" || status === "completed") return "is-success";
  if (status === "system_failed" || status === "canceled") return "is-danger";
  return "";
}

/* Console stage flow — presentation-only timeline derived from run.stage. */
const STAGE_FLOW: RunStage[] = [
  "preparing",
  "inference",
  "citation_fetch",
  "judging",
  "aggregating",
  "publishing",
  "ready_to_publish"
];

function StageTimeline({ stage }: { stage: RunStage }) {
  const currentIndex = Math.max(0, STAGE_FLOW.indexOf(stage));
  return (
    <ol className="run-stage-timeline" aria-label="阶段时间线">
      {STAGE_FLOW.map((key, index) => (
        <li key={key} className={index < currentIndex ? "is-done" : index === currentIndex ? "is-current" : ""} aria-current={index === currentIndex ? "step" : undefined}>
          <i aria-hidden="true" />
          <span>{stageLabels[key]}</span>
        </li>
      ))}
    </ol>
  );
}

function RunConsoleLog({ run }: { run: RunProgress }) {
  const lines: Array<{ time: string; text: string; error?: boolean }> = [
    { time: formatDate(run.created_at), text: `run created · ${runProfileLabel(run)} · ${run.total} 执行项 · 每题 ${run.trial_count} 次` }
  ];
  if (run.started_at) lines.push({ time: formatDate(run.started_at), text: "worker 已接收任务，开始执行矩阵" });
  lines.push({
    time: formatDate(run.heartbeat_at),
    text: `heartbeat · ${stageLabels[run.stage]} · processed ${run.processed}/${run.total} (${run.progress_percent.toFixed(2)}%)`
  });
  if (run.failed > 0 || run.retried > 0) {
    lines.push({ time: formatDate(run.heartbeat_at), text: `${run.failed} 项失败 · ${run.retried} 次重试`, error: run.failed > 0 });
  }
  if (run.finished_at) lines.push({ time: formatDate(run.finished_at), text: `run finished · status=${run.status}` });
  if (run.system_error) lines.push({ time: "-", text: `system_error: ${run.system_error}`, error: true });
  return (
    <div className="run-console-log" role="log" aria-label="运行日志">
      {lines.map((line, index) => (
        <p key={index} className={line.error ? "is-error" : ""}><time>{line.time}</time><span>{line.text}</span></p>
      ))}
    </div>
  );
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
  const [trialCount, setTrialCount] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const experimentSubmission = useRef<{ fingerprint: string; key: string } | null>(null);

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
      const common = {
        name: name.trim(),
        profile_key: profileKey,
        judge_config_id: options.judge_config_id,
        score_config_id: profile.score_config_id,
        ...(forceRetest ? { force_retest: true } : {})
      };
      if (selectedAgentIds.length === 1) {
        const run = await createRun({
          ...common,
          agent_version_ids: [selectedAgentIds[0]],
          ...(trialCount ? { trial_count: Number(trialCount) } : {})
        });
        return { kind: "run" as const, id: run.id };
      }
      const experimentInput = {
        ...common,
        agent_version_ids: selectedAgentIds
      };
      const fingerprint = JSON.stringify(experimentInput);
      if (experimentSubmission.current?.fingerprint !== fingerprint) {
        experimentSubmission.current = { fingerprint, key: randomId() };
      }
      const experiment = await createEvaluationExperiment({
        ...experimentInput,
        idempotency_key: experimentSubmission.current.key
      });
      return { kind: "experiment" as const, id: experiment.id };
    },
    onSuccess: async (created) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["runs"] }),
        queryClient.invalidateQueries({ queryKey: ["evaluation-experiments"] })
      ]);
      onClose();
      navigate(created.kind === "run" ? `/runs/${created.id}` : `/experiments/${created.id}`);
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
      description="选择一个 Agent 创建独立运行；选择两个及以上 Agent 创建可持续比较的评测实验。"
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
           <div className="independent-run-note"><strong>{selectedAgentIds.length >= 2 ? "评测实验模式" : "单 Agent 运行"}</strong><span>{selectedAgentIds.length >= 2 ? "所选 Agent 将进入同一个持久化实验，每个成员仍保有独立运行与证据。" : "选择一个 Agent 时创建独立评测运行。"}</span></div>
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
          <FormField
            label="每题运行次数"
            htmlFor="run-trial-count"
            hint={selectedAgentIds.length >= 2
              ? "评测实验暂按缺省次数运行：计入排行榜的 Profile 默认 5 次，其余默认 1 次。"
              : `留空则按缺省规则：当前 Profile 默认 ${selectedProfile?.ranking_eligible ? 5 : 1} 次。多次运行可统计 pass@1 / pass^k 可靠性指标。`}
          >
            <Select
              id="run-trial-count"
              value={trialCount}
              disabled={selectedAgentIds.length >= 2}
              onChange={(event) => {
                setTrialCount(event.target.value);
                setFormError(null);
              }}
            >
              <option value="">自动（按缺省规则）</option>
              {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((count) => (
                <option key={count} value={String(count)}>{count} 次</option>
              ))}
            </Select>
          </FormField>
          {formError && <div className="result-banner is-error" role="alert">{formError}</div>}
          <div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => setStep(1)}>返回 Agent</Button><Button type="submit">下一步：确认配置</Button></div>
        </form>
      ) : selectedProfile ? (
        <div className="modal-form run-review">
           <div className="review-banner"><span>{selectedAgentIds.length >= 2 ? "READY TO QUEUE · PERSISTED EXPERIMENT" : "READY TO QUEUE · SINGLE RUN"}</span><strong>{name}</strong><small>{selectedAgentIds.length >= 2 ? `以下 ${selectedAgentIds.length} 个 Agent 将在同一实验中比较，每个成员运行可独立查看。` : "将创建一个可独立跟踪与检查证据的评测运行。"}</small></div>
          <dl className="review-metrics">
             <div><dt>{selectedAgentIds.length >= 2 ? "实验成员" : "独立运行"}</dt><dd>{selectedAgentIds.length} 个</dd></div>
            <div><dt>每任务题目</dt><dd>{selectedProfile.question_count}</dd></div>
            <div><dt>每题运行</dt><dd>{selectedAgentIds.length >= 2 ? "按缺省规则" : trialCount ? `${trialCount} 次` : `默认 ${selectedProfile.ranking_eligible ? 5 : 1} 次`}</dd></div>
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
            <div>{selectedProfile.datasets.map((dataset) => <article key={dataset.dataset_version_id}><div><strong>{dataset.name}</strong><small>{metricDefinitionFor(dataset.ability)?.shortLabel ?? dataset.ability} · {dataset.language.toUpperCase()} · V{dataset.version}</small></div><b>{dataset.question_count} 题</b></article>)}</div>
            <footer><span>发布去向</span><strong>{publicationLabel(selectedProfile.publishes_to)}</strong><small>{selectedProfile.ranking_eligible ? "计入排行榜" : "非排名运行，不计入排行榜"}</small></footer>
          </section>
          {!selectedProfile.ranking_eligible && <div className="smoke-review-warning"><strong>Smoke Profile</strong><span>本次结果用于快速验证，不会进入排行榜。</span></div>}
          {duplicateCode === "evaluation_already_active" ? <div className="result-banner is-error" role="alert">所选 Agent 已有相同 Profile 的评测正在排队或运行，不能强制覆盖。请等待现有任务结束后再试。</div> : duplicateCode === "evaluation_result_exists" ? <div className="duplicate-confirm" role="alert"><div><strong>已有可用评测结果</strong><span>重新评测会创建全新独立任务，不会覆盖历史结果。确认后才会强制复测。</span></div><Button type="button" className="is-danger" disabled={mutation.isPending} onClick={() => mutation.mutate(true)}>{mutation.isPending ? "正在重新提交" : "确认重新评测"}</Button></div> : mutation.error ? <div className="result-banner is-error" role="alert">{mutation.error instanceof ApiError ? mutation.error.message : "任务创建失败"}</div> : null}
           <div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => { mutation.reset(); setStep(2); }}>返回修改</Button><Button type="button" disabled={mutation.isPending || Boolean(duplicateCode)} onClick={() => mutation.mutate(false)}>{mutation.isPending ? "正在提交" : selectedAgentIds.length >= 2 ? `创建 ${selectedAgentIds.length} Agent 评测实验` : "创建单 Agent 评测运行"}</Button></div>
        </div>
      ) : null}
    </Modal>
  );
}

function RunsList() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParameters, setSearchParameters] = useSearchParams();
  const [transitionRunId, setTransitionRunId] = useState<string | null>(null);
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
      <div className="stagger-in runs-page-body">
        <PageHeader
          title="评测任务"
          meta={
            <>
              <span>共 <b>{runs.length}</b> 个任务</span>
              <span>执行中 <b>{activeCount}</b></span>
              <span>已发布 <b>{publishedCount}</b></span>
              <span className="runs-poll-note">运行状态每 3 秒同步，页面不可见时自动降频</span>
            </>
          }
          actions={<Button type="button" onClick={openLaunch}>+ 发起评测</Button>}
        />
        {runsQuery.isPending ? (
          <LoadingState label="正在读取评测任务" />
        ) : runsQuery.error ? (
          <div className="table-state is-error">{runsQuery.error.message}</div>
        ) : runs.length === 0 ? (
          <EmptyState
            className="runs-empty"
            title={<h2 className="runs-empty-heading">尚未发起评测</h2>}
            description="选择一个 Agent 创建运行，或选择多个 Agent 创建持久化评测实验。"
            action={<Button type="button" onClick={openLaunch}>发起首个评测</Button>}
          />
        ) : (
          <Table className="runs-table">
            <thead><tr><th>任务</th><th>状态</th><th>处理进度</th><th data-numeric>失败 / 重试</th><th>创建时间</th><th><span className="sr-only">查看</span></th></tr></thead>
            <tbody>{runs.map((run: RunListItem) => {
              const processed = run.completed + run.failed;
              const percent = run.total ? Math.round(processed * 100 / run.total) : 0;
              return (
                <tr key={run.id} className={activeStatuses.has(run.status) ? "is-active-run" : ""}>
                  <td><strong style={transitionRunId === run.id ? { viewTransitionName: "run-detail-name" } as CSSProperties : undefined}>{run.name}</strong><small>{runProfileLabel(run)} · {run.agent_count} Agent · {run.total} 执行项</small></td>
                  <td>
                    <StatusPill tone={runStatusTone(run)} className={runStatusPillClass(run)}>{runStatusLabel(run)}</StatusPill>
                    <small className="stage-copy">{stageLabels[run.stage]}</small>
                  </td>
                  <td>
                    <div className="run-progress-cell">
                      <ProgressBar value={percent} active={activeStatuses.has(run.status)} label={`${run.name} 处理进度`} />
                      <small className="run-progress-copy">{processed} / {run.total} · {percent}%</small>
                    </div>
                  </td>
                  <td data-numeric>{run.failed} / {run.retried}</td>
                  <td data-numeric>{formatDate(run.created_at)}</td>
                  <td>
                    <Link
                      className="row-link"
                      to={`/runs/${run.id}`}
                      onMouseEnter={() => queryClient.prefetchQuery({
                        queryKey: ["run-progress", run.id],
                        queryFn: () => fetchRunProgress(run.id),
                        staleTime: 2_000
                      })}
                      onClick={(event) => {
                        if (!viewTransitionsSupported()) return;
                        event.preventDefault();
                        runSharedElementTransition(
                          () => setTransitionRunId(run.id),
                          () => navigate(`/runs/${run.id}`),
                          () => setTransitionRunId(null)
                        );
                      }}
                    >查看详情 →</Link>
                  </td>
                </tr>
              );
            })}</tbody>
          </Table>
        )}
      </div>
      {launchOpen && <LaunchRunModal initialAgentVersion={quickSelection?.agentVersion} initialProfileKey={quickSelection?.profile} onClose={closeLaunch} />}
    </main>
  );
}

function RunDetail({ runId }: { runId: string }) {
  const navigate = useNavigate();
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

  /* Hero 主角：终态单 Agent 运行突出最终分，其余时刻突出整体进度。 */
  const singleAgentResult = run.agents.length === 1 ? run.agents[0].result : null;
  const heroScore = terminalStatuses.has(run.status) && singleAgentResult ? singleAgentResult.overall_score : null;

  return (
    <main className="workspace run-detail-workspace">
      <div className="stagger-in run-detail-body">
        <Link
          className="back-link"
          to="/runs"
          onClick={(event) => {
            if (!viewTransitionsSupported()) return;
            event.preventDefault();
            runWithViewTransition(() => navigate("/runs"));
          }}
        >← 返回任务列表</Link>
        <PageHeader
          className="run-detail-page-header"
          title={run.name}
          meta={
            <>
              <span>{runProfileLabel(run)}</span>
              <span>{stageLabels[run.stage]}</span>
              <span>每题 {run.trial_count} 次</span>
              <span>创建于 {formatDate(run.created_at)}</span>
              <span className="run-id-copy">RUN / {run.id.slice(0, 8).toUpperCase()}</span>
            </>
          }
          actions={
            <>
              {run.experiment_id && <Link className="button is-ghost" to={`/experiments/${run.experiment_id}`}>所属实验</Link>}
              <Link className="button is-ghost" to={`/runs/${run.id}/evidence`}>查看运行证据</Link>
              {cancelable ? <Button type="button" className="is-danger" onClick={() => setConfirmCancel(true)}>取消任务</Button> : scoringRetryable ? <Button type="button" onClick={() => setConfirmRetryScoring(true)}>重试评分</Button> : null}
            </>
          }
        />

        <section className={`run-hero ${terminalClass(run.status)}`}>
          <div className="run-hero-status">
            <StatusPill tone={runStatusTone(run)} className={runStatusPillClass(run)}>{runStatusLabel(run)}</StatusPill>
            <strong>{run.status === "published" ? publishedMessage(run) : run.status === "system_failed" ? "平台处理发生系统异常，旧榜单未受影响。" : run.status === "canceled" ? "任务已取消，未发布任何结果。" : `当前阶段：${stageLabels[run.stage]}`}</strong>
            {run.system_error && <small>{run.system_error}</small>}
          </div>
          <div className="run-hero-metric">
            <span>{heroScore === null ? "OVERALL PROGRESS" : "最终分 · 综合"}</span>
            <strong>{heroScore === null ? `${run.progress_percent.toFixed(2)}%` : heroScore.toFixed(2)}</strong>
            <p>{run.processed} / {run.total} 执行项已处理</p>
          </div>
          <ProgressBar className="run-detail-progress" value={run.progress_percent} active={activeStatuses.has(run.status)} label="整体处理进度" />
          <dl><div><dt>成功</dt><dd>{run.completed}</dd></div><div><dt>失败</dt><dd>{run.failed}</dd></div><div><dt>重试</dt><dd>{run.retried}</dd></div><div><dt>心跳</dt><dd>{formatDate(run.heartbeat_at)}</dd></div></dl>
        </section>

        <section className="run-console" aria-label="运行控制台输出">
          <header><span>CONSOLE</span><strong>阶段时间线与运行日志</strong></header>
          <StageTimeline stage={run.stage} />
          <RunConsoleLog run={run} />
        </section>

        <section className="agent-progress-grid stagger-in">
        {run.agents.map((agent, index) => {
          const percent = agent.total ? agent.processed * 100 / agent.total : 0;
          return <article key={agent.run_agent_id}><header><span>{String(index + 1).padStart(2, "0")}</span><div><strong><Link to={`/agents/${agent.agent_id}`}>{agent.agent_name}</Link></strong><small>{agent.provider} · V{agent.version}</small></div><Link className="agent-evidence-link" to={`/runs/${run.id}/evidence?run_agent_id=${encodeURIComponent(agent.run_agent_id)}`}>此 Agent 证据</Link></header><div className="agent-progress-value"><strong>{percent.toFixed(0)}%</strong><span>{agent.processed} / {agent.total}</span></div><ProgressBar className="agent-run-progress" value={percent} active={activeStatuses.has(run.status) && agent.processed < agent.total} label={`${agent.agent_name} 处理进度`} /><footer><span>成功 <b>{agent.completed}</b></span><span>失败 <b>{agent.failed}</b></span><span>重试 <b>{agent.retried}</b></span></footer>{agent.validity_status === "invalid" && <div className="result-validity-invalid"><strong>接入配置无效，不进入排行榜</strong><span>{validityReasonLabels[agent.validity_reason ?? ""] ?? agent.validity_reason} · {agent.configuration_failure_count} 项 / {agent.configuration_failure_rate.toFixed(2)}%</span></div>}{agent.result && <section className="run-result-summary six-metric-grid">{leaderboardMetrics.map((metric) => <div key={metric.key}><span>{metric.shortLabel}</span><strong>{metric.key === "overall" ? agent.result?.overall_score.toFixed(2) : agent.result?.ability_scores[metric.key]?.toFixed(2) ?? "—"}</strong></div>)}<footer><span>成功率 {agent.result.success_rate.toFixed(2)}%</span>{agent.result.pass_at_1 !== null && agent.result.pass_at_1 !== undefined && <span>pass@1 {agent.result.pass_at_1.toFixed(2)}%</span>}{agent.result.pass_pow_k !== null && agent.result.pass_pow_k !== undefined && <span>pass^k {agent.result.pass_pow_k.toFixed(2)}%</span>}{agent.result.ci_low !== null && agent.result.ci_low !== undefined && agent.result.ci_high !== null && agent.result.ci_high !== undefined && <span>95% CI {agent.result.ci_low.toFixed(2)}–{agent.result.ci_high.toFixed(2)}</span>}<span>HTTP {agent.result.http_call_count}</span><span>Token {formatCount(agent.result.total_tokens)}</span><span>成本 {formatCostUsd(agent.result.total_cost_usd)}</span><span>$/题 {formatCostUsd(agent.result.cost_per_task_usd)}</span><span>P50/P95 {agent.result.latency_p50_ms ?? "—"}/{agent.result.latency_p95_ms ?? "—"} ms</span></footer></section>}{Object.keys(agent.failure_categories ?? {}).length > 0 && <div className="failure-category-list">{Object.entries(agent.failure_categories).map(([category, count]) => <span key={category}>{failureLabels[category] ?? category}<b>{count}</b></span>)}</div>}</article>;
        })}
        </section>
      </div>

      {confirmCancel && <Modal eyebrow="DESTRUCTIVE ACTION" title="确认取消评测？" description="已完成的执行项会保留，但该批次不会发布榜单。" onClose={() => setConfirmCancel(false)}><div className="modal-form cancel-confirm"><p>任务：<strong>{run.name}</strong></p>{cancelMutation.error && <div className="result-banner is-error">{cancelMutation.error.message}</div>}<div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => setConfirmCancel(false)}>继续运行</Button><Button type="button" className="is-danger" disabled={cancelMutation.isPending} onClick={() => cancelMutation.mutate()}>{cancelMutation.isPending ? "正在取消" : "确认取消任务"}</Button></div></div></Modal>}
      {confirmRetryScoring && <Modal eyebrow="RESUME JUDGING" title="继续未完成的评分？" description="已完成的 Agent 推理和评分会保留，只调用 DeepSeek 处理尚未评分的题目。" onClose={() => setConfirmRetryScoring(false)}><div className="modal-form cancel-confirm"><p>任务：<strong>{run.name}</strong></p>{retryScoringMutation.error && <div className="result-banner is-error">{retryScoringMutation.error.message}</div>}<div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => setConfirmRetryScoring(false)}>暂不重试</Button><Button type="button" disabled={retryScoringMutation.isPending} onClick={() => retryScoringMutation.mutate()}>{retryScoringMutation.isPending ? "正在重新排队" : "确认重试评分"}</Button></div></div></Modal>}
    </main>
  );
}

export function RunsPage() {
  const { runId } = useParams();
  return runId ? <RunDetail runId={runId} /> : <RunsList />;
}
