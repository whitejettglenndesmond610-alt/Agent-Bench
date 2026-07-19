import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { WorkspaceAgent } from "./api";

interface AgentPickerProps {
  agents: WorkspaceAgent[];
  selectedVersionIds: string[];
  recentVersionIds?: string[];
  mode: "single" | "multiple";
  maxSelected?: number;
  disabled?: boolean;
  loading?: boolean;
  error?: string | null;
  label?: string;
  onChange: (versionIds: string[]) => void;
}

function uniqueRecentAgents(agents: WorkspaceAgent[], recentVersionIds: string[]) {
  const byVersion = new Map(agents.map((agent) => [agent.agent_version_id, agent]));
  const seen = new Set<string>();
  return recentVersionIds.flatMap((versionId) => {
    if (seen.has(versionId)) return [];
    seen.add(versionId);
    const agent = byVersion.get(versionId);
    return agent ? [agent] : [];
  });
}

function AgentIdentity({ agent }: { agent: WorkspaceAgent }) {
  const privateAgent = agent.visibility === "private";
  return (
    <span className="agent-picker-identity">
      <i>{agent.name.slice(0, 2).toUpperCase()}</i>
      <span>
        <strong>{agent.name}{privateAgent && <b className="private-api-badge">私人 API</b>}</strong>
        <small>{[agent.provider, agent.model, `V${agent.version}`, agent.supports_citations ? "CITE" : "TEXT"].filter(Boolean).join(" · ")}</small>
      </span>
    </span>
  );
}

export function AgentPicker({
  agents,
  selectedVersionIds,
  recentVersionIds = [],
  mode,
  maxSelected = mode === "single" ? 1 : 5,
  disabled = false,
  loading = false,
  error = null,
  label = mode === "single" ? "选择 Agent" : "选择对比 Agent",
  onChange
}: AgentPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState("all");
  const searchRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const selected = agents.filter((agent) => selectedVersionIds.includes(agent.agent_version_id));
  const providers = [...new Set(agents.map((agent) => agent.provider))].sort((left, right) =>
    left.localeCompare(right, "zh-CN")
  );
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filtered = agents.filter((agent) => {
    const matchesProvider = provider === "all" || agent.provider === provider;
    const haystack = `${agent.name} ${agent.provider} ${agent.model ?? ""} ${agent.description ?? ""}`.toLocaleLowerCase();
    return matchesProvider && (!normalizedQuery || haystack.includes(normalizedQuery));
  });
  const privateAgents = filtered.filter((agent) => agent.visibility === "private");
  const recent = uniqueRecentAgents(
    filtered.filter((agent) => agent.visibility !== "private"),
    recentVersionIds
  );
  const recentIds = new Set(recent.map((agent) => agent.agent_version_id));
  const groups = providers.flatMap((providerName) => {
    const items = filtered.filter(
      (agent) => agent.visibility !== "private"
        && agent.provider === providerName
        && !recentIds.has(agent.agent_version_id)
    );
    return items.length ? [{ provider: providerName, items }] : [];
  });

  useEffect(() => {
    if (!open) return;
    searchRef.current?.focus();
    function trapFocus(event: KeyboardEvent) {
      if (event.key !== "Tab") return;
      const focusable = [...(dialogRef.current?.querySelectorAll<HTMLElement>(
        "button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex='-1'])"
      ) ?? [])];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    window.addEventListener("keydown", trapFocus);
    return () => window.removeEventListener("keydown", trapFocus);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  function closePicker() {
    setOpen(false);
    setTimeout(() => triggerRef.current?.focus(), 0);
  }

  function toggle(versionId: string) {
    if (mode === "single") {
      onChange([versionId]);
      closePicker();
      return;
    }
    if (selectedVersionIds.includes(versionId)) {
      onChange(selectedVersionIds.filter((id) => id !== versionId));
    } else if (selectedVersionIds.length < maxSelected) {
      onChange([...selectedVersionIds, versionId]);
    }
  }

  function remove(versionId: string) {
    onChange(selectedVersionIds.filter((id) => id !== versionId));
  }

  function option(agent: WorkspaceAgent) {
    const checked = selectedVersionIds.includes(agent.agent_version_id);
    const optionDisabled = !checked && selectedVersionIds.length >= maxSelected;
    const accessibleName = `${agent.name}，${agent.provider}${agent.model ? `，模型 ${agent.model}` : ""}，版本 ${agent.version}${agent.visibility === "private" ? "，私人 API" : ""}`;
    if (mode === "single") {
      return (
        <button
          className={checked ? "is-selected" : ""}
          type="button"
          role="option"
          aria-selected={checked}
          aria-label={accessibleName}
          key={agent.agent_version_id}
          onClick={() => toggle(agent.agent_version_id)}
        >
          <AgentIdentity agent={agent} />
          <b>{checked ? "CURRENT" : "SELECT"}</b>
        </button>
      );
    }
    return (
      <label className={checked ? "is-selected" : ""} key={agent.agent_version_id}>
        <input
          type="checkbox"
          checked={checked}
          disabled={optionDisabled}
          aria-label={accessibleName}
          onChange={() => toggle(agent.agent_version_id)}
        />
        <AgentIdentity agent={agent} />
        <b>{checked ? String(selectedVersionIds.indexOf(agent.agent_version_id) + 1).padStart(2, "0") : "+"}</b>
      </label>
    );
  }

  const triggerCopy = loading
    ? "正在读取 Agent 目录"
    : mode === "single"
      ? selected[0]?.name ?? "选择 Agent"
      : selected.length
        ? `已选择 ${selected.length} 个 Agent`
        : "打开 Agent 目录";

  return (
    <div className={`agent-picker-control is-${mode}`}>
      {mode === "multiple" && selected.length > 0 && (
        <div className="agent-picker-selection" aria-label="已选择 Agent">
          {selected.map((agent) => (
            <span key={agent.agent_version_id}>
              {agent.name}
              {agent.visibility === "private" && <b className="private-api-badge">私人 API</b>}
              {agent.model && <small>{agent.model}</small>}
              <button type="button" aria-label={`移除 ${agent.name}`} onClick={() => remove(agent.agent_version_id)}>×</button>
            </span>
          ))}
        </div>
      )}
      <button
        ref={triggerRef}
        className="agent-picker-trigger"
        type="button"
        aria-label={label}
        aria-haspopup="dialog"
        aria-expanded={open}
        disabled={disabled || loading || agents.length === 0}
        onClick={() => setOpen(true)}
      >
        <span>{triggerCopy}{mode === "single" && selected[0]?.visibility === "private" && <b className="private-api-badge">私人 API</b>}</span>
         <small>{mode === "multiple" ? `${selected.length} / ${maxSelected}` : [selected[0]?.model, selected[0]?.provider].filter(Boolean).join(" · ") || "AGENT DIRECTORY"}</small>
        <b>⌄</b>
      </button>
      {error && <small className="agent-picker-error">{error}</small>}
      {open && createPortal(
        <div className="agent-picker-layer" role="presentation">
          <section
            ref={dialogRef}
            className="agent-picker-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="Agent 目录"
          >
            <header>
               <div><span>AGENT DIRECTORY</span><strong>{label}</strong></div>
               <button type="button" aria-label="关闭 Agent 目录" onClick={closePicker}>×</button>
            </header>
            <div className="agent-picker-tools">
              <label>
                 <span className="sr-only">搜索 Agent</span>
                <input
                  ref={searchRef}
                  type="search"
                  value={query}
                   aria-label="搜索 Agent"
                   placeholder="搜索 Agent、提供方、模型或描述"
                  onChange={(event) => setQuery(event.target.value)}
                />
              </label>
              <label>
                 <span className="sr-only">筛选提供方</span>
                 <select aria-label="筛选提供方" value={provider} onChange={(event) => setProvider(event.target.value)}>
                   <option value="all">全部提供方</option>
                  {providers.map((item) => <option value={item} key={item}>{item}</option>)}
                </select>
              </label>
            </div>
            <div
              className="agent-picker-results"
              role={mode === "single" ? "listbox" : "group"}
              aria-label={mode === "multiple" ? "可选 Agent" : undefined}
            >
              {privateAgents.length > 0 && (
                <section>
                  <h3><span>我的 API</span><small>{privateAgents.length} PRIVATE</small></h3>
                  <div>{privateAgents.map(option)}</div>
                </section>
              )}
              {recent.length > 0 && (
                <section>
                  <h3><span>最近使用</span><small>SERVER HISTORY</small></h3>
                  <div>{recent.map(option)}</div>
                </section>
              )}
              {groups.map((group) => (
                <section key={group.provider}>
                   <h3><span>{group.provider}</span><small>{group.items.length} AGENTS</small></h3>
                  <div>{group.items.map(option)}</div>
                </section>
              ))}
              {filtered.length === 0 && <p>没有匹配的 Agent。请调整搜索词或厂商筛选。</p>}
            </div>
            <footer>
              <span>{agents.length} 个可用 Agent · 最近使用来自服务端历史</span>
              {mode === "multiple" && <button type="button" onClick={closePicker}>完成选择（{selected.length} / {maxSelected}）</button>}
            </footer>
          </section>
        </div>,
        document.body
      )}
    </div>
  );
}
