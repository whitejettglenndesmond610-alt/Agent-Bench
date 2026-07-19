import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import {
  ApiError,
  createPrivateAgent,
  deletePrivateAgent,
  fetchPrivateAgents,
  PrivateAgent,
  PrivateAgentCreateInput,
  PrivateAgentList,
  PrivateAgentPreset,
  PrivateAgentUpdateInput,
  testPrivateAgent,
  updatePrivateAgent
} from "./api";
import { Button, FormField, Input, LoadingState, Modal, Select, Textarea } from "./components";
import { formatUserDate, userQueryKeys } from "./userProduct";

const presetLabels: Record<PrivateAgentPreset, string> = {
  dashscope: "阿里百炼",
  deepseek: "DeepSeek",
  openai: "OpenAI"
};

const modelSuggestions: Record<PrivateAgentPreset, string[]> = {
  dashscope: ["qwen-plus", "qwen-max", "qwen-math-turbo", "qwen3.7-plus"],
  deepseek: ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
  openai: ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"]
};

const modelAliases: Record<PrivateAgentPreset, Record<string, string>> = {
  dashscope: { "qwen plus": "qwen-plus", "qwen max": "qwen-max", "qwen math turbo": "qwen-math-turbo", "qwen3.7 plus": "qwen3.7-plus" },
  deepseek: { "deepseek v4 pro": "deepseek-v4-pro", "deepseek v4 flash": "deepseek-v4-flash", "deepseek chat": "deepseek-chat", "deepseek reasoner": "deepseek-reasoner" },
  openai: { "gpt 4.1": "gpt-4.1", "gpt 4.1 mini": "gpt-4.1-mini", "gpt 4o": "gpt-4o", "gpt 4o mini": "gpt-4o-mini" }
};

const healthErrorLabels: Record<string, string> = {
  authentication_failed: "API Key 无效或无权访问该模型",
  balance_exhausted: "账号余额或免费额度不足",
  rate_limited: "提供方请求过于频繁，请稍后重试",
  timeout: "提供方响应超时",
  provider_unavailable: "提供方服务暂时不可用",
  request_rejected: "提供方拒绝了请求，请确认填写的是 API model ID 而不是展示名称",
  network_error: "平台暂时无法连接提供方",
  invalid_response: "提供方返回了无法识别的响应"
};

function normalizeModel(preset: PrivateAgentPreset, model: string) {
  const normalized = model.trim().replace(/\s+/g, " ");
  return modelAliases[preset][normalized.toLocaleLowerCase()] ?? normalized;
}

const healthLabels: Record<PrivateAgent["health_status"], string> = {
  untested: "未测试",
  healthy: "健康",
  unhealthy: "异常",
  revoked: "已撤销"
};

interface PrivateAgentFormValue {
  name: string;
  description: string;
  preset: PrivateAgentPreset;
  model: string;
  apiKey: string;
}

function PrivateAgentForm({
  agent,
  pending,
  error,
  onCancel,
  onSubmit
}: {
  agent?: PrivateAgent;
  pending: boolean;
  error: unknown;
  onCancel: () => void;
  onSubmit: (value: PrivateAgentFormValue) => void;
}) {
  const [name, setName] = useState(agent?.name ?? "");
  const [description, setDescription] = useState(agent?.description ?? "");
  const [preset, setPreset] = useState<PrivateAgentPreset>(agent?.preset ?? "dashscope");
  const [model, setModel] = useState(agent?.model ?? "");
  const [apiKey, setApiKey] = useState("");
  const apiError = error instanceof ApiError ? error : null;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!name.trim() || !model.trim() || (!agent && !apiKey)) return;
    onSubmit({ name: name.trim(), description: description.trim(), preset, model: normalizeModel(preset, model), apiKey });
  }

  return (
    <form className="modal-form my-api-form" onSubmit={submit}>
      <div className="form-grid two-column">
        <FormField label="名称" htmlFor="private-agent-name" required error={apiError?.fieldErrors.name}>
          <Input id="private-agent-name" autoFocus maxLength={120} value={name} onChange={(event) => setName(event.target.value)} />
        </FormField>
        <FormField label="提供方预设" htmlFor="private-agent-preset" required error={apiError?.fieldErrors.preset}>
          <Select id="private-agent-preset" value={preset} onChange={(event) => setPreset(event.target.value as PrivateAgentPreset)}>
            {Object.entries(presetLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </Select>
        </FormField>
        <FormField label="模型 ID" htmlFor="private-agent-model" required hint={`必须填写 API model ID，例如 ${modelSuggestions[preset][0]}，不能填写展示名称。`} error={apiError?.fieldErrors.model}>
          <Input id="private-agent-model" list={`private-model-options-${preset}`} maxLength={200} value={model} placeholder={modelSuggestions[preset][0]} onChange={(event) => setModel(event.target.value)} />
          <datalist id={`private-model-options-${preset}`}>{modelSuggestions[preset].map((item) => <option value={item} key={item} />)}</datalist>
        </FormField>
        <FormField
          label={agent ? "替换 API Key（可选）" : "API Key"}
          htmlFor="private-agent-key"
          required={!agent}
          hint={agent ? "留空将保留并重新加密现有 Key。" : "提交后不会再次显示。"}
          error={apiError?.fieldErrors.api_key}
        >
          <Input
            id="private-agent-key"
            type="password"
            autoComplete="new-password"
            spellCheck={false}
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
          />
        </FormField>
        <FormField label="描述（可选）" htmlFor="private-agent-description" error={apiError?.fieldErrors.description}>
          <Textarea id="private-agent-description" rows={3} maxLength={500} value={description} onChange={(event) => setDescription(event.target.value)} />
        </FormField>
      </div>
      {Boolean(error) && <div className="result-banner is-error" role="alert">{error instanceof Error ? error.message : "保存失败"}</div>}
      <div className="modal-actions">
        <Button type="button" className="is-ghost" onClick={onCancel}>取消</Button>
        <Button type="submit" disabled={pending || !name.trim() || !model.trim() || (!agent && !apiKey)}>
          {pending ? "保存中" : agent ? "保存配置" : "添加 API"}
        </Button>
      </div>
    </form>
  );
}

export function MyApiPage({ userId, isShared }: { userId: string; isShared: boolean }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<PrivateAgent | "create" | null>(null);
  const [deleting, setDeleting] = useState<PrivateAgent | null>(null);
  const [notice, setNotice] = useState("");
  const [noticeIsError, setNoticeIsError] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);
  const agentsQuery = useQuery({
    queryKey: userQueryKeys.privateAgents(userId),
    queryFn: fetchPrivateAgents
  });

  function invalidateAgentCatalogs() {
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: userQueryKeys.privateAgents(userId) }),
      queryClient.invalidateQueries({ queryKey: userQueryKeys.agentOptions(userId) })
    ]);
  }

  function storeAgent(updated: PrivateAgent) {
    queryClient.setQueryData<PrivateAgentList>(userQueryKeys.privateAgents(userId), (current) => ({
      items: current?.items.some((item) => item.agent_id === updated.agent_id)
        ? current.items.map((item) => item.agent_id === updated.agent_id ? updated : item)
        : [updated, ...(current?.items ?? [])]
    }));
  }

  function refreshAfterConflict(error: unknown, close: () => void) {
    if (!(error instanceof ApiError) || !["version_conflict", "private_agent_changed_during_test"].includes(error.code ?? "")) {
      setActionError(error);
      return;
    }
    close();
    setActionError(null);
    setNotice("配置已在其他操作中更新，已重新加载最新状态。");
    setNoticeIsError(false);
    invalidateAgentCatalogs();
  }

  const createMutation = useMutation({
    mutationFn: (input: PrivateAgentCreateInput) => createPrivateAgent(input),
    onSuccess: (created) => {
      storeAgent(created);
      setEditing(null);
      setNotice("API 配置已添加。建议先测试连接再使用。");
      setNoticeIsError(false);
      setActionError(null);
      invalidateAgentCatalogs();
    },
    onError: setActionError
  });
  const updateMutation = useMutation({
    mutationFn: ({ agent, input }: { agent: PrivateAgent; input: PrivateAgentUpdateInput }) =>
      updatePrivateAgent(agent.agent_id, input),
    onSuccess: (updated) => {
      storeAgent(updated);
      setEditing(null);
      setNotice("API 配置已更新。");
      setNoticeIsError(false);
      setActionError(null);
      invalidateAgentCatalogs();
    },
    onError: (error) => refreshAfterConflict(error, () => setEditing(null))
  });
  const testMutation = useMutation({
    mutationFn: (agent: PrivateAgent) => testPrivateAgent(agent.agent_id),
    onSuccess: (result) => {
      storeAgent(result);
      const status = result.status_code == null ? "" : `（HTTP ${result.status_code}）`;
      setNotice(result.success
        ? `连接测试通过，延迟 ${result.latency_ms ?? result.health_latency_ms ?? "--"} ms。`
        : `连接测试未通过：${healthErrorLabels[result.error_code ?? ""] ?? "未知错误"}${status}`);
      setNoticeIsError(!result.success);
      setActionError(null);
      invalidateAgentCatalogs();
    },
    onError: (error) => {
      if (error instanceof ApiError && ["version_conflict", "private_agent_changed_during_test"].includes(error.code ?? "")) {
        refreshAfterConflict(error, () => undefined);
        return;
      }
      setNotice("");
      setNoticeIsError(false);
      setActionError(error);
      invalidateAgentCatalogs();
    }
  });
  const deleteMutation = useMutation({
    mutationFn: (agent: PrivateAgent) => deletePrivateAgent(agent.agent_id, agent.version),
    onSuccess: (_, agent) => {
      queryClient.setQueryData<PrivateAgentList>(userQueryKeys.privateAgents(userId), (current) => ({
        items: current?.items.filter((item) => item.agent_id !== agent.agent_id) ?? []
      }));
      setDeleting(null);
      setNotice("API 配置已撤销并删除。");
      setNoticeIsError(false);
      setActionError(null);
      invalidateAgentCatalogs();
    },
    onError: (error) => refreshAfterConflict(error, () => setDeleting(null))
  });

  function openEditor(value: PrivateAgent | "create") {
    setEditing(value);
    setNotice("");
    setNoticeIsError(false);
    setActionError(null);
  }

  function submitForm(value: PrivateAgentFormValue) {
    if (editing === "create") {
      createMutation.mutate({
        name: value.name,
        ...(value.description ? { description: value.description } : {}),
        preset: value.preset,
        model: value.model,
        api_key: value.apiKey
      });
    } else if (editing) {
      updateMutation.mutate({
        agent: editing,
        input: {
          expected_version: editing.version,
          name: value.name,
          description: value.description,
          preset: value.preset,
          model: value.model,
          ...(value.apiKey ? { api_key: value.apiKey } : {})
        }
      });
    }
  }

  const agents = agentsQuery.data?.items ?? [];
  return (
    <main className="workspace my-api-page">
      <header className="section-header">
        <div>
          <p className="eyebrow">BRING YOUR OWN KEY</p>
          <h1>我的 API</h1>
          <p>连接你自己的模型账号，在在线对话和多 Agent 对比中使用。</p>
        </div>
        <Button onClick={() => openEditor("create")}>+ 添加 API</Button>
      </header>

      <section className="my-api-security" aria-label="API Key 安全说明">
        <strong>{isShared ? "共享会话风险" : "账号与浏览器提醒"}</strong>
        <ul>
          <li>{isShared ? "配置与当前浏览器身份绑定；能使用同一浏览器配置的人也能使用、修改或删除这些配置，请勿在不受信任的设备中添加个人 Key。" : "配置保存在当前账号下；请在可信设备登录，退出账号或清理浏览器站点数据会结束当前访问。"}</li>
          <li>提供方预设的请求地址由平台控制，不能填写自定义端点或请求模板。</li>
          <li>API Key 加密保存且永不回显；更新配置时留空可保留现有 Key。</li>
          <li>你的提示词和对话上下文会发送给所选模型提供方，请遵守其数据与隐私政策。</li>
        </ul>
      </section>

      {notice && <div className={`result-banner ${noticeIsError ? "is-error" : "is-success"}`} role={noticeIsError ? "alert" : "status"}>{notice}</div>}
      {Boolean(actionError) && !editing && !deleting && <div className="result-banner is-error" role="alert">{actionError instanceof Error ? actionError.message : "操作失败"}</div>}
      {agentsQuery.isPending ? <LoadingState label="正在读取私人 API 配置" /> : agentsQuery.error ? (
        <div className="table-state is-error" role="alert">{agentsQuery.error.message}</div>
      ) : agents.length === 0 ? (
        <section className="empty-state"><span>NO PRIVATE API</span><h2>还没有私人 API</h2><p>添加配置并通过连接测试后，即可在工作区选择。</p></section>
      ) : (
        <section className="my-api-grid" aria-label="私人 API 列表">
          {agents.map((agent) => {
            const testing = testMutation.isPending && testMutation.variables?.agent_id === agent.agent_id;
            return (
              <article className="my-api-card" key={agent.agent_id}>
                <header>
                  <div><span>{presetLabels[agent.preset]}</span><h2>{agent.name}</h2></div>
                  <b className={`private-health is-${agent.health_status}`}>{healthLabels[agent.health_status]}</b>
                </header>
                <p>{agent.description || "未填写描述"}</p>
                <dl>
                  <div><dt>提供方</dt><dd>{agent.provider}</dd></div>
                  <div><dt>模型</dt><dd>{agent.model}</dd></div>
                  <div><dt>版本</dt><dd>V{agent.version}</dd></div>
                  <div><dt>工作区</dt><dd>{agent.workspace_available ? "可用" : "不可用"}</dd></div>
                  <div><dt>最近检查</dt><dd>{formatUserDate(agent.health_checked_at, true)}</dd></div>
                  <div><dt>延迟</dt><dd>{agent.health_latency_ms == null ? "--" : `${agent.health_latency_ms} ms`}</dd></div>
                  <div><dt>错误</dt><dd title={agent.health_error_code ?? undefined}>{agent.health_error_code ? healthErrorLabels[agent.health_error_code] ?? agent.health_error_code : "--"}</dd></div>
                </dl>
                <footer>
                  <Button className="is-ghost" disabled={testing} onClick={() => testMutation.mutate(agent)}>{testing ? "测试中" : "测试连接"}</Button>
                  <Button className="is-ghost" onClick={() => openEditor(agent)}>编辑</Button>
                  <Button className="is-danger" onClick={() => { setDeleting(agent); setActionError(null); setNotice(""); }}>撤销并删除</Button>
                  {agent.workspace_available && agent.active_agent_version_id && <span className="my-api-use-links"><small>当前工作区：{agent.active_provider} · {agent.active_model}</small><Link to={`/workspace?agentVersion=${encodeURIComponent(agent.active_agent_version_id)}`}>用于在线对话</Link><Link to={`/compare?agentVersion=${encodeURIComponent(agent.active_agent_version_id)}`}>用于多 Agent 对比</Link></span>}
                </footer>
              </article>
            );
          })}
        </section>
      )}

      {editing && <Modal
        title={editing === "create" ? "添加私人 API" : `编辑 ${editing.name}`}
        description={editing === "create" ? "选择受控提供方预设并保存加密 Key。" : "替换 Key 为可选项，留空不会清除现有 Key。"}
        eyebrow="PRIVATE API"
        onClose={() => setEditing(null)}
      >
        <PrivateAgentForm
          agent={editing === "create" ? undefined : editing}
          pending={editing === "create" ? createMutation.isPending : updateMutation.isPending}
          error={actionError}
          onCancel={() => setEditing(null)}
          onSubmit={submitForm}
        />
      </Modal>}
      {deleting && <Modal title="撤销并删除 API" description="此操作会立即移除工作区可用性，且无法恢复。" eyebrow="REVOKE PRIVATE API" onClose={() => setDeleting(null)}>
        <div className="modal-form my-api-delete-confirm">
          <p>确认撤销并删除 <strong>{deleting.name}</strong>？现有对话记录会保留，但不能再使用此配置生成回答。</p>
          {Boolean(actionError) && <div className="result-banner is-error" role="alert">{actionError instanceof Error ? actionError.message : "删除失败"}</div>}
          <div className="modal-actions"><Button className="is-ghost" onClick={() => setDeleting(null)}>取消</Button><Button className="is-danger" disabled={deleteMutation.isPending} onClick={() => deleteMutation.mutate(deleting)}>{deleteMutation.isPending ? "删除中" : "确认撤销并删除"}</Button></div>
        </div>
      </Modal>}
    </main>
  );
}
