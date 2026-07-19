import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import {
  ApiError,
  createWorkspacePromptTemplate,
  deleteWorkspacePromptTemplate,
  fetchWorkspacePromptTemplates,
  updateWorkspacePromptTemplate,
  WorkspacePromptTemplate,
  WorkspacePromptTemplateList
} from "./api";
import { Button, FormField, Input, Modal, Textarea } from "./components";
import { ClipboardButton, formatUserDate, userQueryKeys } from "./userProduct";

type TemplateDialog = { mode: "create" } | { mode: "edit" | "delete"; template: WorkspacePromptTemplate };

function mutationMessage(error: unknown): string {
  if (error instanceof ApiError && error.code === "revision_conflict") return "模板已在其他位置更新，已刷新列表，请重新操作。";
  if (error instanceof ApiError && error.code === "prompt_template_name_conflict") return "模板名称已存在，请换一个名称。";
  return error instanceof Error ? error.message : "模板操作失败";
}

export function PromptTemplatesPage({ userId }: { userId: string }) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [dialog, setDialog] = useState<TemplateDialog | null>(null);
  const [name, setName] = useState("");
  const [content, setContent] = useState("");
  const [actionError, setActionError] = useState("");
  const templatesQuery = useQuery({
    queryKey: userQueryKeys.templates(userId),
    queryFn: fetchWorkspacePromptTemplates
  });

  function upsert(template: WorkspacePromptTemplate) {
    queryClient.setQueryData<WorkspacePromptTemplateList>(userQueryKeys.templates(userId), (current) => ({
      items: current?.items.some((item) => item.id === template.id)
        ? current.items.map((item) => item.id === template.id ? template : item)
        : [template, ...(current?.items ?? [])]
    }));
  }

  const saveMutation = useMutation({
    mutationFn: () => dialog?.mode === "edit"
      ? updateWorkspacePromptTemplate(dialog.template.id, {
          expected_revision: dialog.template.revision,
          name: name.trim(),
          content
        })
      : createWorkspacePromptTemplate({ name: name.trim(), content }),
    onSuccess: (template) => {
      upsert(template);
      setDialog(null);
      setActionError("");
    },
    onError: (error) => {
      setActionError(mutationMessage(error));
      if (error instanceof ApiError && error.code === "revision_conflict") {
        void queryClient.invalidateQueries({ queryKey: userQueryKeys.templates(userId) });
      }
    }
  });
  const deleteMutation = useMutation({
    mutationFn: (template: WorkspacePromptTemplate) =>
      deleteWorkspacePromptTemplate(template.id, template.revision),
    onSuccess: (_, template) => {
      queryClient.setQueryData<WorkspacePromptTemplateList>(userQueryKeys.templates(userId), (current) => ({
        items: current?.items.filter((item) => item.id !== template.id) ?? []
      }));
      setDialog(null);
      setActionError("");
    },
    onError: (error) => {
      setActionError(mutationMessage(error));
      if (error instanceof ApiError && error.code === "revision_conflict") {
        void queryClient.invalidateQueries({ queryKey: userQueryKeys.templates(userId) });
      }
    }
  });

  function openCreate() {
    setDialog({ mode: "create" });
    setName("");
    setContent("");
    setActionError("");
  }

  function openEdit(template: WorkspacePromptTemplate) {
    setDialog({ mode: "edit", template });
    setName(template.name);
    setContent(template.content);
    setActionError("");
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || !content.trim()) return;
    saveMutation.mutate();
  }

  const normalized = search.trim().toLocaleLowerCase();
  const templates = (templatesQuery.data?.items ?? []).filter((template) =>
    !normalized || `${template.name} ${template.content}`.toLocaleLowerCase().includes(normalized)
  );

  return (
    <main className="workspace templates-page">
      <header className="section-header"><div><p className="eyebrow">PERSONAL PROMPT LIBRARY</p><h1>Prompt 模板</h1><p>保存和复用纯文本提示词。模板不会执行插值、脚本或任何可执行逻辑。</p></div><Button onClick={openCreate}>+ 新建模板</Button></header>
      <section className="templates-tools"><label><span className="sr-only">搜索模板</span><input type="search" aria-label="搜索模板" value={search} placeholder="搜索名称或内容" onChange={(event) => setSearch(event.target.value)} /></label><span>{templates.length} 个模板</span></section>
      {templatesQuery.isPending ? <div className="table-state">正在读取模板...</div> : templatesQuery.error ? <div className="table-state is-error" role="alert">{templatesQuery.error.message}</div> : templates.length ? (
        <section className="template-card-grid">{templates.map((template) => (
          <article key={template.id}>
            <header><div><span>REV {template.revision}</span><h2>{template.name}</h2></div><time dateTime={template.updated_at}>{formatUserDate(template.updated_at)}</time></header>
            <pre>{template.content}</pre>
            <footer>
              <ClipboardButton text={template.content}>复制内容</ClipboardButton>
              <Link to={`/workspace?template=${template.id}`}>用于对话</Link>
              <Link to={`/compare?template=${template.id}`}>用于对比</Link>
              <button type="button" onClick={() => openEdit(template)}>编辑</button>
              <button type="button" className="is-danger" onClick={() => { setDialog({ mode: "delete", template }); setActionError(""); }}>删除</button>
            </footer>
          </article>
        ))}</section>
      ) : <section className="empty-state"><span>PROMPT LIBRARY</span><h2>{search ? "没有匹配的模板" : "建立你的第一个 Prompt 模板"}</h2><p>{search ? "换一个关键词或清除搜索条件。" : "模板只保存原始文本，可随时用于对话或多 Agent 对比。"}</p>{!search && <Button onClick={openCreate}>新建模板</Button>}</section>}

      {dialog?.mode === "delete" && <Modal title="删除 Prompt 模板" description="删除后无法恢复，已创建的对话和对比不受影响。" eyebrow="DELETE TEMPLATE" onClose={() => setDialog(null)}>
        <div className="modal-form template-delete-confirm"><p>确定删除 <strong>{dialog.template.name}</strong>？</p>{actionError && <div className="result-banner is-error" role="alert">{actionError}</div>}<div className="modal-actions"><Button className="is-ghost" onClick={() => setDialog(null)}>取消</Button><Button className="is-danger" disabled={deleteMutation.isPending} onClick={() => deleteMutation.mutate(dialog.template)}>{deleteMutation.isPending ? "删除中" : "确认删除"}</Button></div></div>
      </Modal>}

      {dialog && dialog.mode !== "delete" && <Modal title={dialog.mode === "create" ? "新建 Prompt 模板" : "编辑 Prompt 模板"} description="按原样保存纯文本，不支持变量插值或执行代码。" eyebrow="PROMPT TEMPLATE" onClose={() => setDialog(null)}>
        <form className="modal-form" onSubmit={submit}>
          <div className="form-grid">
            <FormField label="模板名称" htmlFor="template-name" required><Input id="template-name" maxLength={120} value={name} autoFocus onChange={(event) => setName(event.target.value)} /></FormField>
            <FormField label="Prompt 内容" htmlFor="template-content" hint={`${content.length} / 200000 字`} required><Textarea id="template-content" rows={12} maxLength={200_000} value={content} onChange={(event) => setContent(event.target.value)} /></FormField>
          </div>
          {actionError && <div className="result-banner is-error" role="alert">{actionError}</div>}
          <div className="modal-actions"><Button type="button" className="is-ghost" onClick={() => setDialog(null)}>取消</Button><Button type="submit" disabled={saveMutation.isPending || !name.trim() || !content.trim()}>{saveMutation.isPending ? "保存中" : "保存模板"}</Button></div>
        </form>
      </Modal>}
    </main>
  );
}
