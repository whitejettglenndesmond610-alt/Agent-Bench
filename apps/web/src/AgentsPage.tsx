import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import {
  AgentEvaluationMetric,
  AgentEvaluationStatus,
  AgentSummary,
  AgentVersion,
  AgentVersionInput,
  ApiError,
  AuthType,
  ConnectionTestResult,
  createAgent,
  createAgentVersion,
  fetchAgent,
  fetchAgentEvaluationStatuses,
  fetchAgents,
  preflightAgentVersion,
  promoteAgentVersion,
  testAgentConnection
} from "./api";
import { Button, FormField, Input, LoadingState, Modal, Select, Table, Textarea } from "./components";
import { leaderboardMetrics, profileKeyForMetric } from "./metrics";

interface BasicForm {
  name: string;
  provider: string;
  description: string;
  logoUrl: string;
}

interface ConnectionForm {
  endpointUrl: string;
  authType: AuthType;
  token: string;
  requestTemplate: string;
  answerJsonpath: string;
  citationsJsonpath: string;
  timeoutSeconds: number;
  concurrency: number;
  maxPromptCharacters: number | null;
}

const initialConnection: ConnectionForm = {
  endpointUrl: "",
  authType: "none",
  token: "",
  requestTemplate: '{\n  "query": "{{prompt}}"\n}',
  answerJsonpath: "$.answer",
  citationsJsonpath: "$.citations",
  timeoutSeconds: 120,
  concurrency: 5,
  maxPromptCharacters: null
};

const evaluationColumns = leaderboardMetrics.map((metric) => ({
  key: metric.key === "overall" ? "comprehensive" as const : metric.key,
  metricKey: metric.key,
  label: metric.shortLabel
}));

const evaluationStatusLabels = {
  not_evaluated: "未评测",
  queued: "排队中",
  running: "评测中",
  evaluated: "已评测",
  needs_retest: "待复测",
  failed: "失败",
  invalid: "配置无效"
} as const;

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function connectionPayload(form: ConnectionForm): AgentVersionInput {
  return {
    endpoint_url: form.endpointUrl.trim(),
    auth_type: form.authType,
    ...(form.authType === "bearer" ? { token: form.token } : {}),
    request_template: form.requestTemplate,
    answer_jsonpath: form.answerJsonpath.trim(),
    ...(form.citationsJsonpath.trim()
      ? { citations_jsonpath: form.citationsJsonpath.trim() }
      : {}),
    timeout_seconds: form.timeoutSeconds,
    concurrency: form.concurrency,
    max_prompt_characters: form.maxPromptCharacters
  };
}

function ConfigurationModal({
  agent,
  onClose,
  onSaved
}: {
  agent: AgentSummary | null;
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<1 | 2 | 3 | 4>(1);
  const [basic, setBasic] = useState<BasicForm>({
    name: agent?.name ?? "",
    provider: agent?.provider ?? "",
    description: agent?.description ?? "",
    logoUrl: agent?.logo_url ?? ""
  });
  const [connection, setConnection] = useState<ConnectionForm>(initialConnection);
  const [connectionInitialized, setConnectionInitialized] = useState(false);
  const [prompt, setPrompt] = useState("请简要介绍你的核心能力，并给出一个参考链接。");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [createdAgentId, setCreatedAgentId] = useState<string | null>(null);
  const [candidateVersion, setCandidateVersion] = useState<AgentVersion | null>(null);

  const detailQuery = useQuery({
    queryKey: ["agent", agent?.id],
    queryFn: () => {
      if (!agent) throw new Error("Agent 不存在");
      return fetchAgent(agent.id);
    },
    enabled: Boolean(agent)
  });

  function applyApiError(error: unknown, fallbackStep: 1 | 2 | 3) {
    if (!(error instanceof ApiError)) return;
    if (error.code === "agent_name_exists") {
      setFieldErrors({ name: error.message });
      setStep(1);
      return;
    }
    setFieldErrors(error.fieldErrors);
    const fields = Object.keys(error.fieldErrors);
    if (fields.some((field) => ["name", "provider", "description", "logo_url"].includes(field))) {
      setStep(1);
    } else if (fields.length > 0) {
      setStep(fallbackStep);
    }
  }

  const testMutation = useMutation({
    mutationFn: () => testAgentConnection({ ...connectionPayload(connection), prompt }),
    onError: (error) => applyApiError(error, 2)
  });

  const saveMutation = useMutation({
    mutationFn: async () => {
      let agentId = agent?.id ?? createdAgentId;
      if (!agentId) {
        const created = await createAgent({
          name: basic.name.trim(),
          provider: basic.provider.trim(),
          ...(basic.description.trim() ? { description: basic.description.trim() } : {}),
          ...(basic.logoUrl.trim() ? { logo_url: basic.logoUrl.trim() } : {})
        });
        agentId = created.id;
        setCreatedAgentId(created.id);
      }
      return createAgentVersion(agentId, connectionPayload(connection));
    },
    onError: (error) => applyApiError(error, 2),
    onSuccess: async (version) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["agent", version.agent_id] });
      setCandidateVersion(version);
      setStep(4);
    }
  });

  const managedCandidate = candidateVersion ?? detailQuery.data?.versions.find(
    (version) => version.lifecycle_status === "draft" || version.lifecycle_status === "verified"
  ) ?? null;
  const preflightMutation = useMutation({
    mutationFn: () => {
      if (!managedCandidate) throw new Error("候选版本不存在");
      return preflightAgentVersion(managedCandidate.id);
    },
    onSuccess: async (result) => {
      if (managedCandidate) {
        setCandidateVersion({
          ...managedCandidate,
          lifecycle_status: result.lifecycle_status,
          preflight_status: result.preflight_status,
          preflight_report: result.checks,
          preflight_at: result.preflight_at
        });
      }
      await queryClient.invalidateQueries({ queryKey: ["agent", managedCandidate?.agent_id] });
    }
  });
  const promoteMutation = useMutation({
    mutationFn: () => {
      if (!managedCandidate) throw new Error("候选版本不存在");
      return promoteAgentVersion(managedCandidate.id);
    },
    onSuccess: async (version) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["agent-evaluation-statuses"] });
      await queryClient.invalidateQueries({ queryKey: ["agent", version.agent_id] });
      onSaved(`${basic.name} 的 V${version.version} 已设为当前版本`);
    }
  });

  function updateBasic(field: keyof BasicForm, value: string) {
    setBasic((current) => ({ ...current, [field]: value }));
    setFieldErrors((current) => {
      const next = { ...current };
      delete next[field === "logoUrl" ? "logo_url" : field];
      return next;
    });
  }

  function updateConnection<K extends keyof ConnectionForm>(field: K, value: ConnectionForm[K]) {
    setConnection((current) => ({ ...current, [field]: value }));
    setFieldErrors({});
    testMutation.reset();
  }

  function continueToConnection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const latest = detailQuery.data?.versions.find(
      (version) => version.lifecycle_status === "active"
    );
    if (!connectionInitialized && latest) {
      setConnection({
        endpointUrl: latest.endpoint_url,
        authType: latest.auth_type,
        token: "",
        requestTemplate: latest.request_template,
        answerJsonpath: latest.answer_jsonpath,
        citationsJsonpath: latest.citations_jsonpath ?? "",
        timeoutSeconds: latest.timeout_seconds,
        concurrency: latest.concurrency,
        maxPromptCharacters: latest.max_prompt_characters
      });
      setConnectionInitialized(true);
    }
    setStep(2);
  }

  function continueToTest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFieldErrors({});
    setStep(3);
  }

  function runTest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFieldErrors({});
    testMutation.mutate();
  }

  const result: ConnectionTestResult | undefined = testMutation.data;
  const latestVersion = detailQuery.data?.versions.find(
    (version) => version.lifecycle_status === "active"
  );

  return (
    <Modal
      title={agent ? `为 ${agent.name} 创建新版本` : "接入新的 Agent"}
      description="配置不会覆盖历史版本；测试通过后才可保存。"
      onClose={onClose}
    >
      <ol className="stepper" aria-label="配置步骤">
        {["基础信息", "接口配置", "连接测试", "候选预检"].map((label, index) => (
          <li key={label} className={step === index + 1 ? "is-current" : step > index + 1 ? "is-done" : ""}>
            <span>{String(index + 1).padStart(2, "0")}</span>{label}
          </li>
        ))}
      </ol>

      {step === 1 && managedCandidate && (
        <section className="candidate-version-callout">
          <div><span>CANDIDATE VERSION</span><strong>V{managedCandidate.version} · {managedCandidate.preflight_status === "passed" ? "预检通过" : managedCandidate.preflight_status === "failed" ? "预检失败" : "等待预检"}</strong><small>候选版本不会影响当前正式评测和排行榜。</small></div>
          <Button type="button" className="is-quiet" onClick={() => setStep(4)}>管理候选版本</Button>
        </section>
      )}

      {step === 1 && (
        <form className="modal-form" onSubmit={continueToConnection}>
          {agent && (
            <div className="version-context">
              <span>当前版本</span>
              <strong>{detailQuery.isPending ? "读取中" : latestVersion ? `V${latestVersion.version}` : "尚未配置"}</strong>
              <small>新版本会复制公开配置，Bearer Token 不会回显。</small>
            </div>
          )}
          <div className="form-grid">
            <FormField label="Agent 名称" htmlFor="agent-name" error={fieldErrors.name} required>
              <Input id="agent-name" value={basic.name} disabled={Boolean(agent)} maxLength={120} required onChange={(event) => updateBasic("name", event.target.value)} />
            </FormField>
            <FormField label="提供方" htmlFor="agent-provider" error={fieldErrors.provider} required>
              <Input id="agent-provider" value={basic.provider} disabled={Boolean(agent)} maxLength={120} required onChange={(event) => updateBasic("provider", event.target.value)} />
            </FormField>
            <FormField label="Logo URL" htmlFor="agent-logo" error={fieldErrors.logo_url} hint="可选，必须为 HTTP(S) 地址">
              <Input id="agent-logo" type="url" value={basic.logoUrl} disabled={Boolean(agent)} onChange={(event) => updateBasic("logoUrl", event.target.value)} />
            </FormField>
            <FormField label="说明" htmlFor="agent-description" error={fieldErrors.description}>
              <Textarea id="agent-description" rows={3} value={basic.description} disabled={Boolean(agent)} onChange={(event) => updateBasic("description", event.target.value)} />
            </FormField>
          </div>
          <div className="modal-actions"><Button type="button" className="is-ghost" onClick={onClose}>取消</Button><Button type="submit" disabled={Boolean(agent) && detailQuery.isPending}>下一步：接口配置</Button></div>
        </form>
      )}

      {step === 2 && (
        <form className="modal-form" onSubmit={continueToTest}>
          <div className="form-grid two-column">
            <FormField label="接口地址" htmlFor="endpoint-url" error={fieldErrors.endpoint_url} required>
              <Input id="endpoint-url" type="url" placeholder="https://agent.example.com/v1/chat" value={connection.endpointUrl} required onChange={(event) => updateConnection("endpointUrl", event.target.value)} />
            </FormField>
            <FormField label="鉴权方式" htmlFor="auth-type" error={fieldErrors.auth_type} required>
              <Select id="auth-type" value={connection.authType} onChange={(event) => updateConnection("authType", event.target.value as AuthType)}><option value="none">无鉴权</option><option value="bearer">Bearer Token</option></Select>
            </FormField>
            {connection.authType === "bearer" && (
              <FormField label="Bearer Token" htmlFor="agent-token" error={fieldErrors.token} hint={agent ? "历史 Token 已加密保存；创建版本时需重新输入。" : "Token 只会加密保存，不会在页面中回显。"} required>
                <Input id="agent-token" type="password" autoComplete="new-password" value={connection.token} required onChange={(event) => updateConnection("token", event.target.value)} />
              </FormField>
            )}
            <FormField label="超时时间（秒）" htmlFor="timeout-seconds" error={fieldErrors.timeout_seconds}>
              <Input id="timeout-seconds" type="number" min={1} max={600} value={connection.timeoutSeconds} onChange={(event) => updateConnection("timeoutSeconds", Number(event.target.value))} />
            </FormField>
            <FormField label="并发数" htmlFor="concurrency" error={fieldErrors.concurrency} hint="演示默认 5 路并发">
              <Input id="concurrency" type="number" min={1} max={20} value={connection.concurrency} onChange={(event) => updateConnection("concurrency", Number(event.target.value))} />
            </FormField>
            <FormField label="最大 Prompt 字符数" htmlFor="max-prompt-characters" error={fieldErrors.max_prompt_characters} hint="可选；超过限制的 Profile 会在创建任务前被阻止">
              <Input id="max-prompt-characters" type="number" min={1} max={200000} placeholder="不限制" value={connection.maxPromptCharacters ?? ""} onChange={(event) => updateConnection("maxPromptCharacters", event.target.value ? Number(event.target.value) : null)} />
            </FormField>
            <FormField label="请求 JSON 模板" htmlFor="request-template" error={fieldErrors.request_template} hint="仅支持 {{prompt}} 变量" required>
              <Textarea id="request-template" className="code-control" rows={7} value={connection.requestTemplate} required onChange={(event) => updateConnection("requestTemplate", event.target.value)} />
            </FormField>
            <FormField label="答案 JSONPath" htmlFor="answer-jsonpath" error={fieldErrors.answer_jsonpath} required>
              <Input id="answer-jsonpath" className="code-control" value={connection.answerJsonpath} aria-invalid={Boolean(fieldErrors.answer_jsonpath)} required onChange={(event) => updateConnection("answerJsonpath", event.target.value)} />
            </FormField>
            <FormField label="引用 JSONPath" htmlFor="citations-jsonpath" error={fieldErrors.citations_jsonpath} hint="可选，目标应为 URL 数组">
              <Input id="citations-jsonpath" className="code-control" value={connection.citationsJsonpath} onChange={(event) => updateConnection("citationsJsonpath", event.target.value)} />
            </FormField>
          </div>
          <div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => setStep(1)}>返回</Button><Button type="submit">下一步：连接测试</Button></div>
        </form>
      )}

      {step === 3 && (
        <div className="modal-form test-step">
          <form className="test-prompt" onSubmit={runTest}>
            <FormField label="测试问题" htmlFor="test-prompt" error={fieldErrors.prompt} required>
              <Textarea id="test-prompt" rows={3} value={prompt} required onChange={(event) => { setPrompt(event.target.value); testMutation.reset(); }} />
            </FormField>
            <Button type="submit" disabled={testMutation.isPending}>{testMutation.isPending ? "正在请求 Agent" : "发送测试请求"}</Button>
          </form>

          {testMutation.error && <div className="result-banner is-error" role="alert">{testMutation.error.message}</div>}
          {result && (
            <section className={`test-result ${result.success ? "is-success" : "is-error"}`} aria-label="连接测试结果">
              <header><div><span>{result.success ? "TEST PASSED" : "TEST FAILED"}</span><strong>{result.success ? "连接与提取成功" : result.error}</strong></div><dl><div><dt>HTTP</dt><dd>{result.status_code ?? "-"}</dd></div><div><dt>耗时</dt><dd>{result.latency_ms} ms</dd></div></dl></header>
              <div className="result-grid">
                <div><span>提取答案</span><p>{result.answer ?? "未提取"}</p></div>
                <div><span>引用 URL</span>{result.citations.length ? <ul>{result.citations.map((citation) => <li key={citation}>{citation}</li>)}</ul> : <p>未提取</p>}</div>
              </div>
              <details><summary>查看请求体与原始响应</summary><div className="payload-grid"><pre>{JSON.stringify(result.request_body, null, 2)}</pre><pre>{JSON.stringify(result.raw_response, null, 2)}</pre></div></details>
            </section>
          )}

          <div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => setStep(2)}>返回修改</Button><Button type="button" disabled={!result?.success || saveMutation.isPending} onClick={() => saveMutation.mutate()}>{saveMutation.isPending ? "保存中" : agent ? "保存为新版本" : "创建 Agent 并保存"}</Button></div>
          {saveMutation.error && <div className="result-banner is-error" role="alert">{saveMutation.error.message}</div>}
        </div>
      )}

      {step === 4 && managedCandidate && (
        <div className="modal-form candidate-step">
          <div className="candidate-summary"><div><span>CANDIDATE / V{managedCandidate.version}</span><strong>{managedCandidate.preflight_status === "passed" ? "预检通过，可以晋升" : managedCandidate.preflight_status === "failed" ? "预检未通过" : preflightMutation.isPending ? "正在执行自动预检" : "等待自动预检"}</strong><small>{basic.provider}</small></div><b>{managedCandidate.max_prompt_characters ? `≤ ${managedCandidate.max_prompt_characters} 字符` : "Prompt 不限"}</b></div>
          <section className="preflight-checks" aria-label="候选版本预检结果">
            {managedCandidate.preflight_report.length ? managedCandidate.preflight_report.map((check) => <article className={`is-${check.status}`} key={check.key}><i /><div><strong>{check.label}</strong><small>{check.prompt_characters} 字符{check.latency_ms === null ? "" : ` · ${check.latency_ms} ms`}{check.status_code === null ? "" : ` · HTTP ${check.status_code}`}</small>{check.error && <span>{check.error_category ?? "unknown"} · {check.error}</span>}</div><b>{check.status === "passed" ? "通过" : check.status === "failed" ? "失败" : "跳过"}</b></article>) : <p>自动预检会执行基础连接、代表性长题和引用提取检查，最多调用 Agent 三次。</p>}
          </section>
          {(preflightMutation.error || promoteMutation.error) && <div className="result-banner is-error" role="alert">{preflightMutation.error?.message ?? promoteMutation.error?.message}</div>}
          <div className="candidate-actions"><Button type="button" className="is-ghost" disabled={preflightMutation.isPending || promoteMutation.isPending} onClick={() => onSaved(`${basic.name} 的 V${managedCandidate.version} 候选版本已保存，尚未生效`)}>稍后处理</Button>{managedCandidate.preflight_status === "passed" && <a className="button is-ghost" href={`/runs?launch=1&agentVersion=${encodeURIComponent(managedCandidate.id)}&profile=smoke`}>运行 Smoke</a>}<Button type="button" className="is-ghost" disabled={preflightMutation.isPending || promoteMutation.isPending} onClick={() => preflightMutation.mutate()}>{preflightMutation.isPending ? "预检进行中" : managedCandidate.preflight_status === "failed" ? "重新预检" : "运行自动预检"}</Button><Button type="button" disabled={managedCandidate.preflight_status !== "passed" || preflightMutation.isPending || promoteMutation.isPending} onClick={() => promoteMutation.mutate()}>{promoteMutation.isPending ? "正在晋升" : "设为当前版本"}</Button></div>
        </div>
      )}
    </Modal>
  );
}

function EvaluationCell({
  agent,
  evaluation,
  metric,
  profileMetric,
  metricLabel
}: {
  agent: AgentSummary;
  evaluation?: AgentEvaluationStatus;
  metric: AgentEvaluationMetric | undefined;
  profileMetric: (typeof leaderboardMetrics)[number]["key"];
  metricLabel: string;
}) {
  const status = metric?.status ?? "not_evaluated";
  const active = status === "queued" || status === "running";
  const quickUrl = evaluation
    ? `/runs?launch=1&agentVersion=${encodeURIComponent(evaluation.agent_version_id)}&profile=${encodeURIComponent(profileKeyForMetric(profileMetric))}`
    : undefined;
  const score = status === "evaluated" && metric?.score != null
    ? Number.isInteger(metric.score) ? metric.score.toFixed(0) : metric.score.toFixed(1)
    : null;

  return (
    <div className={`evaluation-cell is-${status}`}>
      {active && metric?.run_id ? (
        <a className="evaluation-state" href={`/runs/${metric.run_id}`} aria-label={`${agent.name} ${metricLabel}${evaluationStatusLabels[status]}，查看任务`}>
          <i />{evaluationStatusLabels[status]}
        </a>
      ) : (
        <span className="evaluation-state"><i />{score ?? evaluationStatusLabels[status]}</span>
      )}
      {!active && agent.status === "enabled" && quickUrl && (
        <a className="quick-evaluate" href={quickUrl} aria-label={`评测 ${agent.name} 的${metricLabel}能力`}>评测 →</a>
      )}
    </div>
  );
}

export function AgentsPage() {
  const agentsQuery = useQuery({ queryKey: ["agents"], queryFn: fetchAgents });
  const statusesQuery = useQuery({
    queryKey: ["agent-evaluation-statuses"],
    queryFn: fetchAgentEvaluationStatuses
  });
  const [modalOpen, setModalOpen] = useState(false);
  const [selectedAgent, setSelectedAgent] = useState<AgentSummary | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const agents = agentsQuery.data ?? [];
  const statusesByAgent = new Map(
    (statusesQuery.data ?? []).map((status) => [status.agent_id, status])
  );

  function openConfiguration(agent: AgentSummary | null) {
    setSelectedAgent(agent);
    setModalOpen(true);
    setNotice(null);
  }

  return (
    <main className="workspace agents-workspace">
      <header className="section-header">
        <div><p className="eyebrow">AGENT CENTER</p><h1>Agent 中心</h1><p>管理外部 Agent API、验证响应映射，并以不可变版本追踪配置与评测状态。</p></div>
        <Button type="button" onClick={() => openConfiguration(null)}>+ 接入 Agent</Button>
      </header>

      {notice && <div className="result-banner is-success" role="status">{notice}</div>}

      <section className="registry-summary" aria-label="Agent 接入统计">
        <div><span>已登记</span><strong>{agents.length}</strong></div>
        <div><span>启用中</span><strong>{agents.filter((agent) => agent.status === "enabled").length}</strong></div>
        <p>所有凭据使用 AES-GCM 加密，仅在发起请求时短暂解密。</p>
      </section>

      {agentsQuery.isPending || statusesQuery.isPending ? (
        <LoadingState label="正在读取 Agent" />
      ) : agentsQuery.error || statusesQuery.error ? (
        <div className="table-state is-error">{agentsQuery.error?.message ?? statusesQuery.error?.message}</div>
      ) : agents.length === 0 ? (
        <section className="empty-state"><span>00</span><h2>尚未接入 Agent</h2><p>创建第一个接口配置并发送真实测试请求。</p><Button type="button" onClick={() => openConfiguration(null)}>开始接入</Button></section>
      ) : (
        <Table className="agents-table">
          <thead><tr><th>Agent</th><th>版本 / 状态</th>{evaluationColumns.map((column) => <th key={column.key}>{column.label}</th>)}<th><span className="sr-only">操作</span></th></tr></thead>
          <tbody>{agents.map((agent) => {
            const evaluation = statusesByAgent.get(agent.id);
            const constraints = evaluation
              ? [evaluation.max_prompt_characters ? `≤ ${evaluation.max_prompt_characters} 字符` : "Prompt 不限", evaluation.supports_citations ? "支持引用" : "无引用"]
              : [];
            return <tr key={agent.id}><td><a className="agent-name-link" href={`/agents/${agent.id}`}>{agent.name}</a><small>{agent.provider} · {agent.description || "暂无说明"}</small></td><td><span className={`registry-status ${agent.status === "enabled" ? "is-enabled" : ""}`}><i />{agent.status === "enabled" ? "启用" : "停用"}</span><small className="agent-version-copy">{evaluation ? `V${evaluation.version} · ${constraints.join(" · ")}` : `更新于 ${formatDate(agent.updated_at)}`}</small></td>{evaluationColumns.map((column) => <td className="evaluation-metric" key={column.key}><EvaluationCell agent={agent} evaluation={evaluation} metric={evaluation?.[column.key]} profileMetric={column.metricKey} metricLabel={column.label} /></td>)}<td><div className="agent-row-actions"><a href={`/agents/${agent.id}`}>查看详情</a><Button type="button" className="is-quiet" onClick={() => openConfiguration(agent)}>配置新版本</Button></div></td></tr>;
          })}</tbody>
        </Table>
      )}

      {modalOpen && <ConfigurationModal key={selectedAgent?.id ?? "new"} agent={selectedAgent} onClose={() => setModalOpen(false)} onSaved={(message) => { setModalOpen(false); setNotice(message); }} />}
    </main>
  );
}
