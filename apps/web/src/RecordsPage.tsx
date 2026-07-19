import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  ApiError,
  createWorkspaceComparison,
  fetchWorkspaceComparison,
  fetchWorkspaceComparisons,
  fetchWorkspaceConversation,
  fetchWorkspaceConversations,
  updateWorkspaceComparison,
  updateWorkspaceConversation,
  WorkspaceComparison,
  WorkspaceComparisonList,
  WorkspaceConversation,
  WorkspaceConversationList
} from "./api";
import { Button, Input, Modal } from "./components";
import {
  comparisonMarkdown,
  conversationMarkdown,
  downloadJson,
  downloadMarkdown,
  formatUserDate,
  userQueryKeys
} from "./userProduct";

type RecordEntry =
  | { type: "conversation"; item: WorkspaceConversation }
  | { type: "comparison"; item: WorkspaceComparison };
type RecordDialog = { action: "rename" | "archive"; record: RecordEntry };

function isArchived(record: RecordEntry) {
  return Boolean(record.item.archived_at);
}

function recordStatus(record: RecordEntry) {
  if (record.item.archived_at) return "已归档";
  if (record.type === "conversation") return "可继续";
  return record.item.status === "completed" ? "已完成" : "进行中";
}

function revisionConflict(error: unknown) {
  return error instanceof ApiError && error.code === "revision_conflict";
}

export function RecordsPage({ userId }: { userId: string }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [type, setType] = useState("all");
  const [status, setStatus] = useState("all");
  const [dialog, setDialog] = useState<RecordDialog | null>(null);
  const [title, setTitle] = useState("");
  const [actionError, setActionError] = useState("");
  const [exportingId, setExportingId] = useState("");
  const conversationsQuery = useQuery({
    queryKey: userQueryKeys.conversations(userId),
    queryFn: fetchWorkspaceConversations
  });
  const comparisonsQuery = useQuery({
    queryKey: userQueryKeys.comparisons(userId),
    queryFn: () => fetchWorkspaceComparisons(true)
  });

  function updateConversationCache(updated: WorkspaceConversation) {
    queryClient.setQueryData<WorkspaceConversationList>(userQueryKeys.conversations(userId), (current) => ({
      items: current?.items.map((item) => item.id === updated.id ? updated : item) ?? [updated]
    }));
  }

  function updateComparisonCache(updated: WorkspaceComparison) {
    queryClient.setQueryData<WorkspaceComparisonList>(userQueryKeys.comparisons(userId), (current) => ({
      items: current?.items.map((item) => item.id === updated.id ? updated : item) ?? [updated]
    }));
    queryClient.setQueryData(userQueryKeys.comparison(userId, updated.id), updated);
  }

  const updateMutation = useMutation({
    mutationFn: async ({ record, nextTitle, archived }: {
      record: RecordEntry;
      nextTitle?: string;
      archived?: boolean;
    }) => record.type === "conversation"
      ? updateWorkspaceConversation(record.item.id, {
          expected_revision: record.item.revision,
          ...(nextTitle === undefined ? {} : { title: nextTitle }),
          ...(archived === undefined ? {} : { archived })
        })
      : updateWorkspaceComparison(record.item.id, {
          expected_revision: record.item.revision ?? 0,
          ...(nextTitle === undefined ? {} : { title: nextTitle }),
          ...(archived === undefined ? {} : { archived })
        }),
    onSuccess: (updated, variables) => {
      if (variables.record.type === "conversation") updateConversationCache(updated as WorkspaceConversation);
      else updateComparisonCache(updated as WorkspaceComparison);
      setDialog(null);
      setActionError("");
    },
    onError: (error) => {
      setActionError(revisionConflict(error) ? "记录已在其他位置更新，列表已刷新，请重新操作。" : error instanceof Error ? error.message : "记录更新失败");
      if (revisionConflict(error)) {
        void queryClient.invalidateQueries({ queryKey: ["workspace", userId] });
      }
    }
  });

  const rerunMutation = useMutation({
    mutationFn: async (comparison: WorkspaceComparison) => {
      const detail = await fetchWorkspaceComparison(comparison.id);
      return createWorkspaceComparison({
        prompt: detail.prompt,
        agent_version_ids: [...detail.items]
          .sort((left, right) => left.position - right.position)
          .map((item) => item.agent.agent_version_id),
        title: `${detail.title} · 再次运行`,
        idempotency_key: crypto.randomUUID()
      });
    },
    onSuccess: (created) => {
      updateComparisonCache(created);
      navigate(`/compare/${created.id}`);
    },
    onError: (error) => setActionError(error instanceof Error ? error.message : "再次运行失败")
  });

  async function exportRecord(record: RecordEntry, format: "markdown" | "json") {
    setExportingId(`${record.type}-${record.item.id}-${format}`);
    setActionError("");
    try {
      if (record.type === "conversation") {
        const detail = await queryClient.fetchQuery({
          queryKey: userQueryKeys.conversation(userId, record.item.id),
          queryFn: () => fetchWorkspaceConversation(record.item.id)
        });
        if (format === "json") downloadJson(detail.title, detail);
        else downloadMarkdown(detail.title, conversationMarkdown(detail));
      } else {
        const detail = await queryClient.fetchQuery({
          queryKey: userQueryKeys.comparison(userId, record.item.id),
          queryFn: () => fetchWorkspaceComparison(record.item.id)
        });
        if (format === "json") downloadJson(detail.title, detail);
        else downloadMarkdown(detail.title, comparisonMarkdown(detail));
      }
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "导出失败");
    } finally {
      setExportingId("");
    }
  }

  function beginRename(record: RecordEntry) {
    setDialog({ action: "rename", record });
    setTitle(record.item.title);
    setActionError("");
  }

  function submitRename(event: FormEvent) {
    event.preventDefault();
    if (!dialog || dialog.action !== "rename" || !title.trim()) return;
    updateMutation.mutate({ record: dialog.record, nextTitle: title.trim() });
  }

  const normalized = search.trim().toLocaleLowerCase();
  const records: RecordEntry[] = [
    ...(conversationsQuery.data?.items ?? []).map((item): RecordEntry => ({ type: "conversation", item })),
    ...(comparisonsQuery.data?.items ?? []).map((item): RecordEntry => ({ type: "comparison", item }))
  ].filter((record) => {
    const matchesSearch = !normalized || `${record.item.title} ${record.type === "conversation" ? record.item.agent.name : record.item.items.map((item) => item.agent.name).join(" ")}`.toLocaleLowerCase().includes(normalized);
    const matchesType = type === "all" || record.type === type;
    const matchesStatus = status === "all"
      || (status === "active" && !isArchived(record))
      || (status === "archived" && isArchived(record))
      || (status === "running" && record.type === "comparison" && record.item.status !== "completed")
      || (status === "completed" && record.type === "comparison" && record.item.status === "completed");
    return matchesSearch && matchesType && matchesStatus;
  }).sort((left, right) => {
    const timeDifference = Date.parse(right.item.updated_at) - Date.parse(left.item.updated_at);
    return timeDifference || left.item.id.localeCompare(right.item.id);
  });
  const activeRecords = records.filter((record) => !isArchived(record));
  const archivedRecords = records.filter(isArchived);
  const pending = conversationsQuery.isPending || comparisonsQuery.isPending;
  const loadError = conversationsQuery.error ?? comparisonsQuery.error;

  function renderRecord(record: RecordEntry) {
    const href = record.type === "conversation" ? `/workspace/${record.item.id}` : `/compare/${record.item.id}`;
    const agents = record.type === "conversation" ? record.item.agent.name : record.item.items.map((item) => item.agent.name).join("、");
    return <article className="record-card" key={`${record.type}-${record.item.id}`}>
      <div className="record-type-mark"><span>{record.type === "conversation" ? "CHAT" : "COMPARE"}</span><b>{record.type === "conversation" ? "对话" : "多 Agent 对比"}</b></div>
      <div className="record-main"><Link to={href}>{record.item.title}</Link><p>{agents || "Agent 信息暂不可用"}</p><small>{recordStatus(record)} · 更新于 {formatUserDate(record.item.updated_at)}</small></div>
      <div className="record-actions">
        <Link to={href}>{record.type === "conversation" && !record.item.archived_at ? "继续" : "打开"}</Link>
        <button type="button" onClick={() => beginRename(record)}>重命名</button>
        <button type="button" onClick={() => { setDialog({ action: "archive", record }); setActionError(""); }}>{record.item.archived_at ? "恢复" : "归档"}</button>
        {record.type === "comparison" && <button type="button" disabled={rerunMutation.isPending} onClick={() => rerunMutation.mutate(record.item)}>再次运行</button>}
        <button type="button" disabled={Boolean(exportingId)} onClick={() => void exportRecord(record, "markdown")}>导出 Markdown</button>
        <button type="button" disabled={Boolean(exportingId)} onClick={() => void exportRecord(record, "json")}>导出 JSON</button>
      </div>
    </article>;
  }

  return (
    <main className="workspace records-page">
      <header className="section-header"><div><p className="eyebrow">PERSONAL ACTIVITY</p><h1>我的记录</h1><p>统一查找、继续、整理和导出你的单 Agent 会话与多 Agent 对比。</p></div><div className="record-header-actions"><Link className="button" to="/workspace">新建对话</Link><Link className="button is-ghost" to="/compare">新建对比</Link></div></header>
      <section className="record-filters" aria-label="记录筛选">
        <label><span>搜索</span><input type="search" aria-label="搜索记录" value={search} placeholder="标题或 Agent" onChange={(event) => setSearch(event.target.value)} /></label>
        <label><span>类型</span><select aria-label="筛选记录类型" value={type} onChange={(event) => setType(event.target.value)}><option value="all">全部类型</option><option value="conversation">在线对话</option><option value="comparison">多 Agent 对比</option></select></label>
        <label><span>状态</span><select aria-label="筛选记录状态" value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">全部状态</option><option value="active">未归档</option><option value="archived">已归档</option><option value="running">对比进行中</option><option value="completed">对比已完成</option></select></label>
        <strong>{records.length} 条记录</strong>
      </section>
      {actionError && <div className="result-banner is-error" role="alert">{actionError}</div>}
      {pending ? <div className="table-state">正在整理个人记录...</div> : loadError ? <div className="table-state is-error" role="alert">{loadError.message}</div> : records.length ? <div className="records-sections">
        {activeRecords.length > 0 && <section><header><h2>进行中与近期记录</h2><span>{activeRecords.length}</span></header><div>{activeRecords.map(renderRecord)}</div></section>}
        {archivedRecords.length > 0 && <section className="is-archived"><header><h2>已归档</h2><span>{archivedRecords.length}</span></header><div>{archivedRecords.map(renderRecord)}</div></section>}
      </div> : <section className="empty-state"><span>NO RECORD</span><h2>{search || type !== "all" || status !== "all" ? "没有符合条件的记录" : "还没有个人记录"}</h2><p>开始一次对话或多 Agent 对比，结果会持续保存在这里。</p></section>}

      {dialog?.action === "rename" && <Modal title="重命名记录" description="标题会在导航和导出文件中使用。" eyebrow="RENAME RECORD" onClose={() => setDialog(null)}><form className="modal-form" onSubmit={submitRename}><label className="form-field"><span>新标题</span><Input aria-label="记录标题" maxLength={200} value={title} autoFocus onChange={(event) => setTitle(event.target.value)} /></label>{actionError && <div className="result-banner is-error" role="alert">{actionError}</div>}<div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => setDialog(null)}>取消</Button><Button type="submit" disabled={updateMutation.isPending || !title.trim()}>{updateMutation.isPending ? "保存中" : "保存标题"}</Button></div></form></Modal>}
      {dialog?.action === "archive" && <Modal title={dialog.record.item.archived_at ? "恢复记录" : "归档记录"} description={dialog.record.item.archived_at ? "恢复后记录会回到活跃区域。" : "归档会把记录移至已归档区域，但不会删除问题、回答或运行数据。"} eyebrow="RECORD STATUS" onClose={() => setDialog(null)}><div className="modal-form"><p>确认{dialog.record.item.archived_at ? "恢复" : "归档"} <strong>{dialog.record.item.title}</strong>？</p>{actionError && <div className="result-banner is-error" role="alert">{actionError}</div>}<div className="modal-actions"><Button className="is-ghost" onClick={() => setDialog(null)}>取消</Button><Button disabled={updateMutation.isPending} onClick={() => updateMutation.mutate({ record: dialog.record, archived: !dialog.record.item.archived_at })}>{updateMutation.isPending ? "更新中" : "确认"}</Button></div></div></Modal>}
    </main>
  );
}
