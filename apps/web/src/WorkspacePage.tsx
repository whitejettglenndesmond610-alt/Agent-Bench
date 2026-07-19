import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  ReactNode,
  useEffect,
  useRef,
  useState
} from "react";
import ReactMarkdown from "react-markdown";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

import {
  ApiError,
  cancelWorkspaceAttempt,
  createEvaluationCase,
  createWorkspaceConversation,
  EvaluationCaseList,
  fetchEvaluationCases,
  fetchWorkspaceAgentOptions,
  fetchWorkspaceConversation,
  fetchWorkspaceConversations,
  fetchWorkspaceExperienceTask,
  fetchWorkspacePromptTemplates,
  regenerateWorkspaceMessage,
  sendWorkspaceMessage,
  updateWorkspaceConversation,
  WorkspaceAgent,
  WorkspaceAttempt,
  WorkspaceConversation,
  WorkspaceConversationDetail,
  WorkspaceConversationList,
  WorkspaceRegenerateResult,
  WorkspaceSendResult,
  WorkspaceTurn
} from "./api";
import { AgentPicker } from "./AgentPicker";
import { LoadingState } from "./components";
import {
  AttemptAnnotationControls,
  ClipboardButton,
  conversationMarkdown,
  downloadJson,
  downloadMarkdown,
  userQueryKeys
} from "./userProduct";

const activeStatuses = new Set(["queued", "running"]);
const workspaceConversationsKey = (userId: string) => ["workspace", userId, "conversations"] as const;
const workspaceConversationKey = (userId: string, conversationId: string) =>
  ["workspace", userId, "conversation", conversationId] as const;
const workspaceEvaluationCasesKey = (userId: string) =>
  ["workspace", userId, "evaluation-cases"] as const;
const workspaceEvaluationCaseKey = (userId: string, caseId: string) =>
  ["workspace", userId, "evaluation-case", caseId] as const;

function isActiveAttempt(attempt: WorkspaceAttempt): boolean {
  return activeStatuses.has(attempt.status);
}

function mergeAttempt(turn: WorkspaceTurn, attempt: WorkspaceAttempt): WorkspaceTurn {
  const attempts = turn.attempts.some((item) => item.id === attempt.id)
    ? turn.attempts.map((item) => item.id === attempt.id ? attempt : item)
    : [...turn.attempts, attempt];
  const assistant = attempt.assistant_message;
  const assistantMessages = assistant
    ? turn.assistant_messages.some((message) => message.id === assistant.id)
      ? turn.assistant_messages.map((message) => message.id === assistant.id ? assistant : message)
      : [...turn.assistant_messages, assistant]
    : turn.assistant_messages;
  return { ...turn, attempts, assistant_messages: assistantMessages };
}

function reconcileSendResult(
  conversation: WorkspaceConversationDetail,
  result: WorkspaceSendResult
): WorkspaceConversationDetail {
  const turnNo = result.user_message.turn_no || conversation.last_turn_no + 1;
  const turnIndex = conversation.turns.findIndex((turn) => turn.user_message.id === result.user_message.id);
  const newTurn: WorkspaceTurn = {
    turn_no: turnNo,
    user_message: result.user_message,
    assistant_messages: result.attempt.assistant_message ? [result.attempt.assistant_message] : [],
    attempts: [result.attempt]
  };
  const turns = turnIndex === -1
    ? [...conversation.turns, newTurn]
    : conversation.turns.map((turn, index) => index === turnIndex ? mergeAttempt(turn, result.attempt) : turn);
  return {
    ...conversation,
    revision: result.conversation_revision,
    last_turn_no: Math.max(conversation.last_turn_no, turnNo),
    updated_at: result.user_message.created_at,
    turns
  };
}

function reconcileRegenerateResult(
  conversation: WorkspaceConversationDetail,
  userMessageId: string,
  result: WorkspaceRegenerateResult
): WorkspaceConversationDetail {
  return {
    ...conversation,
    revision: result.conversation_revision,
    turns: conversation.turns.map((turn) =>
      turn.user_message.id === userMessageId ? mergeAttempt(turn, result.attempt) : turn
    )
  };
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "--";
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

function safeHref(href: string | undefined): string | undefined {
  if (!href) return undefined;
  if (href.startsWith("#") || href.startsWith("/")) return href;
  try {
    const url = new URL(href, window.location.origin);
    return ["http:", "https:", "mailto:"].includes(url.protocol)
      && !url.username
      && !url.password
      ? href
      : undefined;
  } catch {
    return undefined;
  }
}

function isExternalHref(href: string): boolean {
  try {
    const url = new URL(href, window.location.origin);
    return url.protocol === "mailto:" || url.origin !== window.location.origin;
  } catch {
    return false;
  }
}

function copyText(text: string) {
  if (navigator.clipboard) void navigator.clipboard.writeText(text).catch(() => undefined);
}

function CodeBlock({ children }: { children: ReactNode }) {
  const codeRef = useRef<HTMLPreElement>(null);
  return (
    <div className="workspace-code-block">
      <button type="button" onClick={() => copyText(codeRef.current?.innerText ?? "")}>复制代码</button>
      <pre ref={codeRef}>{children}</pre>
    </div>
  );
}

export function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="workspace-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          a: ({ href, children, title }) => {
            const safe = safeHref(href);
            if (!safe) return <span>{children}</span>;
            const external = isExternalHref(safe);
            return (
              <a
                href={safe}
                title={title}
                target={external ? "_blank" : undefined}
                rel={external ? "noopener noreferrer" : undefined}
                referrerPolicy={external ? "no-referrer" : undefined}
              >
                {children}
              </a>
            );
          },
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
          table: ({ children }) => (
            <div className="workspace-table-scroll"><table>{children}</table></div>
          ),
          img: ({ alt }) => (
            <span className="markdown-image-placeholder">{alt ? `[图片: ${alt}]` : "[远程图片已隐藏]"}</span>
          )
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function citationParts(citation: string): { label: string; href?: string } {
  const looksLikeLink = /^(https?:\/\/|mailto:|\/|#)/i.test(citation);
  return { label: citation, href: looksLikeLink ? safeHref(citation) : undefined };
}

export function Citations({ citations }: { citations: string[] }) {
  if (!citations.length) return null;
  return (
    <section className="workspace-citations" aria-label="引用来源">
      <span>CITATIONS / {String(citations.length).padStart(2, "0")}</span>
      <ol>
        {citations.map((citation, index) => {
          const { label, href } = citationParts(citation);
          return (
            <li key={`${label}-${index}`}>
              {href ? (
                <a
                  href={href}
                  target={isExternalHref(href) ? "_blank" : undefined}
                  rel={isExternalHref(href) ? "noopener noreferrer" : undefined}
                  referrerPolicy={isExternalHref(href) ? "no-referrer" : undefined}
                >
                  {label}
                </a>
              ) : label}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

const attemptLabels: Record<WorkspaceAttempt["status"], string> = {
  queued: "等待调度",
  running: "正在生成",
  succeeded: "生成完成",
  failed: "生成失败",
  canceled: "已停止"
};

function attemptLabel(attempt: WorkspaceAttempt): string {
  return attempt.status === "running" && attempt.cancel_requested_at
    ? "正在停止"
    : attemptLabels[attempt.status];
}

function AttemptMeta({ attempt }: { attempt: WorkspaceAttempt | undefined }) {
  if (!attempt) return null;
  const tokenCopy = attempt.total_tokens == null
    ? "TOKEN --"
    : `TOKEN ${attempt.total_tokens} · IN ${attempt.input_tokens ?? "--"} · OUT ${attempt.output_tokens ?? "--"}`;
  return (
    <div className="attempt-meta">
      <span>GEN {String(attempt.generation_no).padStart(2, "0")}</span>
      <span>{attempt.latency_ms == null ? "LATENCY --" : `${attempt.latency_ms} MS`}</span>
      <span>{tokenCopy}</span>
      <span>{formatTime(attempt.finished_at || attempt.started_at)}</span>
    </div>
  );
}

function AttemptStage({
  attempt,
  latestTurn,
  busy,
  onCancel,
  onRetry
}: {
  attempt: WorkspaceAttempt;
  latestTurn: boolean;
  busy: boolean;
  onCancel: (attemptId: string) => void;
  onRetry: () => void;
}) {
  const active = isActiveAttempt(attempt);
  return (
    <section
      className={`attempt-stage is-${attempt.status}`}
      role={attempt.status === "failed" ? "alert" : "status"}
      aria-live="polite"
    >
      <i />
      <div>
        <span>GENERATION / {String(attempt.generation_no).padStart(2, "0")}</span>
        <strong>{attemptLabel(attempt)}</strong>
        {attempt.error && (
          <small>错误分类 {attempt.error.category} · {attempt.error.type}</small>
        )}
      </div>
      {active && (
        <button
          type="button"
          disabled={busy || Boolean(attempt.cancel_requested_at)}
          onClick={() => onCancel(attempt.id)}
        >
          {attempt.cancel_requested_at ? "停止中" : "停止"}
        </button>
      )}
      {(attempt.status === "failed" || attempt.status === "canceled") && latestTurn && (
        <button type="button" disabled={busy} onClick={onRetry}>重试生成</button>
      )}
      {!active && attempt.status !== "failed" && attempt.status !== "canceled" && <AttemptMeta attempt={attempt} />}
    </section>
  );
}

function TurnLedger({
  userId,
  turn,
  latestTurn,
  selectedVariant,
  busy,
  canDraft,
  evaluationCaseIdsByAttempt,
  evaluationCasesReady,
  draftingAttemptId,
  onSelectVariant,
  onCancel,
  onCreateDraft,
  onRegenerate
}: {
  userId: string;
  turn: WorkspaceTurn;
  latestTurn: boolean;
  selectedVariant: number | undefined;
  busy: boolean;
  canDraft: boolean;
  evaluationCaseIdsByAttempt: Record<string, string>;
  evaluationCasesReady: boolean;
  draftingAttemptId: string | undefined;
  onSelectVariant: (variant: number) => void;
  onCancel: (attemptId: string) => void;
  onCreateDraft: (attemptId: string) => void;
  onRegenerate: () => void;
}) {
  const messages = [...turn.assistant_messages].sort((left, right) => left.variant_no - right.variant_no);
  const activeMessage = messages.find((message) => message.active) ?? messages.at(-1);
  const selectedMessage = messages.find((message) => message.variant_no === selectedVariant) ?? activeMessage;
  const selectedIndex = selectedMessage ? messages.findIndex((message) => message.id === selectedMessage.id) : -1;
  const attempts = [...turn.attempts].sort((left, right) => left.generation_no - right.generation_no);
  const latestAttempt = attempts.at(-1);
  const selectedAttempt = selectedMessage
    ? attempts.find((attempt) => attempt.assistant_message_id === selectedMessage.id)
      ?? attempts.find((attempt) => attempt.generation_no === selectedMessage.variant_no)
    : latestAttempt;
  const evaluationCaseId = selectedAttempt
    ? evaluationCaseIdsByAttempt[selectedAttempt.id]
    : undefined;
  const pendingNewAttempt = latestAttempt
    && latestAttempt.id !== selectedAttempt?.id
    && (isActiveAttempt(latestAttempt) || latestAttempt.status === "failed" || latestAttempt.status === "canceled");

  return (
    <article className="ledger-turn">
      <section className="ledger-user-message">
        <header>
          <span>YOU / TURN {String(turn.turn_no).padStart(2, "0")}</span>
          <time dateTime={turn.user_message.created_at}>{formatTime(turn.user_message.created_at)}</time>
        </header>
        <p>{turn.user_message.content}</p>
        <ClipboardButton className="copy-prompt-button" text={turn.user_message.content}>复制提问</ClipboardButton>
      </section>

      {selectedMessage && (
        <section className="ledger-agent-message">
          <header className="agent-message-header">
            <div><i /><span>AGENT RESPONSE / V{String(selectedMessage.variant_no).padStart(2, "0")}</span></div>
            {messages.length > 1 && (
              <div className="variant-controls" aria-label={`第 ${turn.turn_no} 轮回答版本`}>
                <button
                  type="button"
                  aria-label="上一个回答"
                  disabled={selectedIndex <= 0}
                  onClick={() => onSelectVariant(messages[selectedIndex - 1].variant_no)}
                >
                  ←
                </button>
                <span>{selectedIndex + 1} / {messages.length}</span>
                <button
                  type="button"
                  aria-label="下一个回答"
                  disabled={selectedIndex >= messages.length - 1}
                  onClick={() => onSelectVariant(messages[selectedIndex + 1].variant_no)}
                >
                  →
                </button>
              </div>
            )}
          </header>
          <MarkdownContent content={selectedMessage.content} />
          <Citations citations={selectedMessage.citations ?? []} />
          <footer className="agent-message-footer">
            <AttemptMeta attempt={selectedAttempt} />
            <div>
              <ClipboardButton text={selectedMessage.content}>复制回答</ClipboardButton>
              {canDraft && selectedAttempt?.status === "succeeded" && (
                evaluationCaseId ? (
                  <Link to={`/evaluation-cases/${evaluationCaseId}`}>查看评测草稿</Link>
                ) : evaluationCasesReady ? (
                  <button
                    type="button"
                    disabled={Boolean(draftingAttemptId)}
                    onClick={() => onCreateDraft(selectedAttempt.id)}
                  >
                    {draftingAttemptId === selectedAttempt.id ? "保存中" : "保存为评测草稿"}
                  </button>
                ) : null
              )}
              {latestTurn && !latestAttempt?.status.match(/queued|running/) && (
                <button type="button" disabled={busy} onClick={onRegenerate}>重新生成</button>
              )}
            </div>
          </footer>
          {selectedAttempt?.status === "succeeded" && (
            <AttemptAnnotationControls userId={userId} attempt={selectedAttempt} />
          )}
        </section>
      )}

      {latestAttempt && (!selectedMessage || pendingNewAttempt) && (
        <AttemptStage
          attempt={latestAttempt}
          latestTurn={latestTurn}
          busy={busy}
          onCancel={onCancel}
          onRetry={onRegenerate}
        />
      )}
    </article>
  );
}

function ConversationRail({
  conversations,
  activeId,
  loading,
  error,
  editingId,
  editTitle,
  mutationPending,
  onNew,
  onClose,
  onStartRename,
  onEditTitle,
  onRename,
  onArchive
}: {
  conversations: WorkspaceConversation[];
  activeId: string | undefined;
  loading: boolean;
  error: string | null;
  editingId: string | null;
  editTitle: string;
  mutationPending: boolean;
  onNew: () => void;
  onClose: () => void;
  onStartRename: (conversation: WorkspaceConversation) => void;
  onEditTitle: (title: string) => void;
  onRename: (conversation: WorkspaceConversation) => void;
  onArchive: (conversation: WorkspaceConversation, archived: boolean) => void;
}) {
  const live = conversations.filter((conversation) => !conversation.archived_at);
  const archived = conversations.filter((conversation) => conversation.archived_at);

  function renderItem(conversation: WorkspaceConversation) {
    const editing = editingId === conversation.id;
    return (
      <li className={conversation.id === activeId ? "is-active" : ""} key={conversation.id}>
        {editing ? (
          <form
            className="rail-rename-form"
            onSubmit={(event) => {
              event.preventDefault();
              onRename(conversation);
            }}
          >
            <label className="sr-only" htmlFor={`rename-${conversation.id}`}>会话标题</label>
            <input
              id={`rename-${conversation.id}`}
              value={editTitle}
              maxLength={200}
              autoFocus
              onChange={(event) => onEditTitle(event.target.value)}
            />
            <button type="submit" disabled={mutationPending || !editTitle.trim()}>保存</button>
          </form>
        ) : (
          <>
            <Link to={`/workspace/${conversation.id}`} onClick={onClose}>
              <strong>{conversation.title || "未命名会话"}</strong>
              <span>{conversation.agent.name} · {conversation.agent.visibility === "private" ? "私人 API · " : ""}{conversation.agent.model ? `${conversation.agent.model} · ` : ""}V{conversation.agent.version}</span>
              <time dateTime={conversation.updated_at}>{formatTime(conversation.updated_at)}</time>
            </Link>
            <div className="rail-item-actions">
              <button type="button" aria-label={`重命名 ${conversation.title}`} onClick={() => onStartRename(conversation)}>改名</button>
              <button
                type="button"
                aria-label={`${conversation.archived_at ? "恢复" : "归档"} ${conversation.title}`}
                disabled={mutationPending}
                onClick={() => onArchive(conversation, !conversation.archived_at)}
              >
                {conversation.archived_at ? "恢复" : "归档"}
              </button>
            </div>
          </>
        )}
      </li>
    );
  }

  return (
    <div className="conversation-rail-inner">
      <header>
        <div><span>CONVERSATION INDEX</span><strong>会话档案</strong></div>
        <button type="button" className="new-conversation-button" onClick={onNew}>+ 新对话</button>
        <button type="button" className="conversation-rail-close" aria-label="关闭会话列表" onClick={onClose}>×</button>
      </header>
      <div className="rail-scroll">
        {loading && <p className="rail-state">正在读取会话记录</p>}
        {error && <p className="rail-state is-error" role="alert">会话历史读取失败：{error}</p>}
        {!loading && !error && live.length === 0 && <p className="rail-state">暂无进行中的会话</p>}
        {live.length > 0 && <ul>{live.map(renderItem)}</ul>}
        {archived.length > 0 && (
          <section className="archived-conversations">
            <span>ARCHIVED / {String(archived.length).padStart(2, "0")}</span>
            <ul>{archived.map(renderItem)}</ul>
          </section>
        )}
      </div>
      <footer><span>SERVER PERSISTED</span><i />NO LOCAL CACHE</footer>
    </div>
  );
}

export function WorkspacePage({ userId, canDraft }: { userId: string; canDraft: boolean }) {
  const { conversationId } = useParams();
  const [searchParameters] = useSearchParams();
  const requestedAgentVersionId = searchParameters.get("agentVersion") ?? "";
  const requestedTemplateId = searchParameters.get("template") ?? "";
  const requestedTaskSlug = searchParameters.get("task") ?? "";
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [selectedAgentVersionId, setSelectedAgentVersionId] = useState(requestedAgentVersionId);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [variantSelection, setVariantSelection] = useState<Record<number, number>>({});
  const [actionError, setActionError] = useState<string | null>(null);
  const [blockedConversationId, setBlockedConversationId] = useState<string | null>(null);
  const [mobileDrawer, setMobileDrawer] = useState(
    () => window.matchMedia?.("(max-width: 900px)").matches ?? false
  );
  const drawerToggleRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const ledgerRef = useRef<HTMLDivElement>(null);
  const ledgerEndRef = useRef<HTMLDivElement>(null);
  const pinnedToBottomRef = useRef(true);
  const forceScrollRef = useRef(false);
  const sendLockRef = useRef(false);
  const routeConversationRef = useRef(conversationId);
  const draftTouchedRef = useRef(false);
  const prefillHandledRef = useRef("");

  const catalogQuery = useQuery({
    queryKey: userQueryKeys.agentOptions(userId),
    queryFn: fetchWorkspaceAgentOptions
  });
  const conversationsQuery = useQuery({
    queryKey: workspaceConversationsKey(userId),
    queryFn: fetchWorkspaceConversations
  });
  const templatesQuery = useQuery({
    queryKey: userQueryKeys.templates(userId),
    queryFn: fetchWorkspacePromptTemplates
  });
  const taskQuery = useQuery({
    queryKey: userQueryKeys.task(userId, requestedTaskSlug || "none"),
    queryFn: () => fetchWorkspaceExperienceTask(requestedTaskSlug),
    enabled: Boolean(requestedTaskSlug),
    retry: false
  });
  const conversationQuery = useQuery({
    queryKey: workspaceConversationKey(userId, conversationId ?? "new"),
    queryFn: () => fetchWorkspaceConversation(conversationId!),
    enabled: Boolean(conversationId),
    refetchInterval: (query) => {
      const conversation = query.state.data;
      return conversation?.turns.some((turn) => turn.attempts.some(isActiveAttempt)) ? 1_000 : false;
    }
  });
  const evaluationCasesQuery = useQuery({
    queryKey: workspaceEvaluationCasesKey(userId),
    queryFn: fetchEvaluationCases,
    enabled: canDraft
  });

  const catalog = catalogQuery.data ?? [];
  const conversations = conversationsQuery.data?.items ?? [];
  const conversation = conversationQuery.isError ? undefined : conversationQuery.data;
  const selectedAgent = conversation
    ? catalog.find((agent) => agent.agent_version_id === conversation.agent.agent_version_id)
    : !conversationId
      ? catalog.find((agent) => agent.agent_version_id === selectedAgentVersionId) ?? catalog[0]
      : undefined;
  const migrationTarget = conversation && !selectedAgent
    ? catalog.find((agent) => agent.agent_id === conversation.agent.agent_id)
    : undefined;
  const activeAttempt = conversation?.turns
    .flatMap((turn) => turn.attempts)
    .find(isActiveAttempt);
  const maximumCharacters = selectedAgent?.max_prompt_characters ?? null;
  const archived = Boolean(conversation?.archived_at);
  const detailUnavailable = Boolean(conversationId) && !conversation;
  const detailBlocked = Boolean(conversationId && blockedConversationId === conversationId);
  const evaluationCaseIdsByAttempt = Object.fromEntries(
    (evaluationCasesQuery.data?.items ?? []).map((item) => [item.source.attempt_id, item.id])
  );

  useEffect(() => {
    routeConversationRef.current = conversationId;
    setDraft("");
    setVariantSelection({});
    setActionError(null);
    setBlockedConversationId(null);
    setEditingId(null);
    setEditTitle("");
    pinnedToBottomRef.current = true;
    forceScrollRef.current = false;
    sendLockRef.current = false;
    draftTouchedRef.current = false;
    if (ledgerRef.current) ledgerRef.current.scrollTop = 0;
  }, [conversationId]);

  useEffect(() => {
    if (!requestedTemplateId && !requestedTaskSlug) return;
    const key = `${conversationId ?? "new"}:${requestedTemplateId}:${requestedTaskSlug}`;
    if (prefillHandledRef.current === key) return;
    const template = requestedTemplateId
      ? templatesQuery.data?.items.find((item) => item.id === requestedTemplateId)
      : undefined;
    const loading = (requestedTemplateId && templatesQuery.isPending) || (requestedTaskSlug && taskQuery.isPending);
    if (loading) return;
    prefillHandledRef.current = key;
    const content = template?.content ?? taskQuery.data?.prompt;
    if (content && !draftTouchedRef.current && !draft) setDraft(content);
  }, [conversationId, draft, requestedTaskSlug, requestedTemplateId, taskQuery.data, taskQuery.isPending, templatesQuery.data, templatesQuery.isPending]);

  useEffect(() => {
    if (!window.matchMedia) return;
    const query = window.matchMedia("(max-width: 900px)");
    function updateMobile(event: MediaQueryListEvent) {
      setMobileDrawer(event.matches);
    }
    setMobileDrawer(query.matches);
    query.addEventListener("change", updateMobile);
    return () => query.removeEventListener("change", updateMobile);
  }, []);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 190)}px`;
  }, [draft]);

  useEffect(() => {
    if (!conversationQuery.data || (!pinnedToBottomRef.current && !forceScrollRef.current)) return;
    ledgerEndRef.current?.scrollIntoView?.({
      behavior: forceScrollRef.current ? "smooth" : "auto",
      block: "end"
    });
    forceScrollRef.current = false;
  }, [conversationQuery.data]);

  function invalidateWorkspace(id?: string) {
    void queryClient.invalidateQueries({ queryKey: workspaceConversationsKey(userId) });
    if (id) void queryClient.invalidateQueries({ queryKey: workspaceConversationKey(userId, id) });
  }

  function upsertConversation(item: WorkspaceConversation) {
    queryClient.setQueryData<WorkspaceConversationList>(
      workspaceConversationsKey(userId),
      (current) => {
        if (!current) return { items: [item] };
        const exists = current.items.some((conversation) => conversation.id === item.id);
        return {
          items: exists
            ? current.items.map((conversation) => conversation.id === item.id ? { ...conversation, ...item } : conversation)
            : [item, ...current.items]
        };
      }
    );
  }

  function routeIsCurrent(originConversationId: string | undefined) {
    return routeConversationRef.current === originConversationId;
  }

  async function handleMutationError(
    error: unknown,
    originConversationId: string | undefined,
    fallback: string,
    targetConversationId = originConversationId
  ) {
    const apiError = error instanceof ApiError ? error : null;
    const message = apiError?.code === "prompt_too_long"
      ? "对话完整上下文超过此 Agent 的 Prompt 限制，请缩短消息或新建会话。"
      : apiError?.code === "revision_conflict"
        ? "会话已在其他位置更新，正在同步；请稍后重试。"
        : error instanceof Error ? error.message : fallback;
    if (routeIsCurrent(originConversationId)) setActionError(message);
    if (apiError?.status !== 409 || apiError.code !== "revision_conflict" || !targetConversationId) return;

    if (routeIsCurrent(targetConversationId)) setBlockedConversationId(targetConversationId);
    await Promise.all([
      queryClient.refetchQueries({ queryKey: workspaceConversationKey(userId, targetConversationId), exact: true }),
      queryClient.refetchQueries({ queryKey: workspaceConversationsKey(userId), exact: true })
    ]);
    const detailState = queryClient.getQueryState(workspaceConversationKey(userId, targetConversationId));
    if (detailState?.status === "success" && routeIsCurrent(targetConversationId)) {
      setBlockedConversationId(null);
    }
  }

  const createMutation = useMutation({
    mutationFn: ({ agent }: { agent: WorkspaceAgent; originConversationId: string | undefined }) =>
      createWorkspaceConversation({ agent_version_id: agent.agent_version_id }),
    onSuccess: (created, variables) => {
      upsertConversation(created);
      if (routeIsCurrent(variables.originConversationId)) {
        setActionError(null);
        navigate(`/workspace/${created.id}`);
      }
      invalidateWorkspace(created.id);
    },
    onError: (error, variables) => void handleMutationError(error, variables.originConversationId, "创建会话失败")
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, revision, title, archived: nextArchived, agentVersionId }: {
      id: string;
      revision: number;
      title?: string;
      archived?: boolean;
      agentVersionId?: string;
      originConversationId: string | undefined;
    }) => updateWorkspaceConversation(id, {
      ...(title !== undefined ? { title } : {}),
      ...(nextArchived !== undefined ? { archived: nextArchived } : {}),
      ...(agentVersionId !== undefined ? { agent_version_id: agentVersionId } : {}),
      expected_revision: revision
    }),
    onSuccess: (updated, variables) => {
      upsertConversation(updated);
      queryClient.setQueryData<WorkspaceConversationDetail>(
        workspaceConversationKey(userId, updated.id),
        (current) => current ? { ...current, ...updated, turns: current.turns } : current
      );
      if (routeIsCurrent(variables.originConversationId)) {
        setActionError(null);
        setEditingId(null);
      }
      invalidateWorkspace(updated.id);
      if (variables.archived && variables.id === variables.originConversationId && routeIsCurrent(variables.originConversationId)) {
        navigate("/workspace");
      }
    },
    onError: (error, variables) => void handleMutationError(
      error,
      variables.originConversationId,
      "更新会话失败",
      variables.id
    )
  });

  const sendMutation = useMutation({
    mutationFn: async ({ content, originConversationId, sourceConversation, agent }: {
      content: string;
      originConversationId: string | undefined;
      sourceConversation: WorkspaceConversationDetail | undefined;
      agent: WorkspaceAgent | undefined;
    }) => {
      let id: string;
      let revision: number;
      let createdConversation: WorkspaceConversation | undefined;
      if (originConversationId) {
        if (!sourceConversation || sourceConversation.id !== originConversationId) {
          throw new Error("会话尚未加载，请稍后重试");
        }
        id = sourceConversation.id;
        revision = sourceConversation.revision;
      } else {
        if (!agent) throw new Error("请先选择一个 Agent");
        const created = await createWorkspaceConversation({
          agent_version_id: agent.agent_version_id
        });
        id = created.id;
        revision = created.revision;
        createdConversation = created;
      }
      const result = await sendWorkspaceMessage(id, {
        content,
        idempotency_key: crypto.randomUUID(),
        expected_revision: revision
      });
      return { id, result, createdConversation, sourceConversation };
    },
    onMutate: () => {
      setActionError(null);
      forceScrollRef.current = true;
    },
    onSuccess: ({ id, result, createdConversation, sourceConversation }, variables) => {
      const baseConversation = sourceConversation ?? (createdConversation ? { ...createdConversation, turns: [] } : undefined);
      if (baseConversation) {
        const reconciled = reconcileSendResult(baseConversation, result);
        queryClient.setQueryData(workspaceConversationKey(userId, id), reconciled);
        upsertConversation(reconciled);
      }
      if (routeIsCurrent(variables.originConversationId)) {
        setDraft("");
        setActionError(null);
        if (createdConversation) navigate(`/workspace/${id}`, { replace: true });
      }
      invalidateWorkspace(id);
    },
    onError: (error, variables) => void handleMutationError(error, variables.originConversationId, "发送失败")
  });

  const regenerateMutation = useMutation({
    mutationFn: ({ id, userMessageId, revision }: {
      id: string;
      userMessageId: string;
      revision: number;
      originConversationId: string;
      sourceConversation: WorkspaceConversationDetail;
    }) => regenerateWorkspaceMessage(id, userMessageId, {
        idempotency_key: crypto.randomUUID(),
        expected_revision: revision
      }),
    onMutate: () => {
      setActionError(null);
      forceScrollRef.current = true;
    },
    onSuccess: (result, variables) => {
      const current = queryClient.getQueryData<WorkspaceConversationDetail>(
        workspaceConversationKey(userId, variables.id)
      ) ?? variables.sourceConversation;
      const reconciled = reconcileRegenerateResult(current, variables.userMessageId, result);
      queryClient.setQueryData(workspaceConversationKey(userId, variables.id), reconciled);
      upsertConversation(reconciled);
      if (routeIsCurrent(variables.originConversationId)) setActionError(null);
      invalidateWorkspace(variables.id);
    },
    onError: (error, variables) => void handleMutationError(error, variables.originConversationId, "重新生成失败")
  });

  const cancelMutation = useMutation({
    mutationFn: ({ attemptId }: { attemptId: string; originConversationId: string }) =>
      cancelWorkspaceAttempt(attemptId),
    onSuccess: (attempt, variables) => {
      queryClient.setQueryData<WorkspaceConversationDetail>(
        workspaceConversationKey(userId, variables.originConversationId),
        (current) => current ? {
          ...current,
          turns: current.turns.map((turn) =>
            turn.attempts.some((item) => item.id === attempt.id) ? mergeAttempt(turn, attempt) : turn
          )
        } : current
      );
      if (routeIsCurrent(variables.originConversationId)) setActionError(null);
      invalidateWorkspace(variables.originConversationId);
    },
    onError: (error, variables) => void handleMutationError(error, variables.originConversationId, "停止生成失败")
  });

  const createDraftMutation = useMutation({
    mutationFn: ({ attemptId }: { attemptId: string; originConversationId: string }) =>
      createEvaluationCase({
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
      if (routeIsCurrent(variables.originConversationId)) {
        navigate(`/evaluation-cases/${evaluationCase.id}`);
      }
    },
    onError: (error, variables) => void handleMutationError(
      error,
      variables.originConversationId,
      "保存评测草稿失败"
    )
  });

  const conversationMutationPending = (
    createMutation.isPending && createMutation.variables?.originConversationId === conversationId
  ) || (
    updateMutation.isPending && updateMutation.variables?.originConversationId === conversationId
  ) || (
    sendMutation.isPending && sendMutation.variables?.originConversationId === conversationId
  ) || (
    regenerateMutation.isPending && regenerateMutation.variables?.originConversationId === conversationId
  ) || (
    cancelMutation.isPending && cancelMutation.variables?.originConversationId === conversationId
  );

  function closeDrawer(returnFocus = false) {
    const shouldRestoreFocus = returnFocus && drawerOpen;
    setDrawerOpen(false);
    if (shouldRestoreFocus) setTimeout(() => drawerToggleRef.current?.focus(), 0);
  }

  function openDrawer() {
    setDrawerOpen(true);
    setTimeout(() => {
      drawerRef.current?.querySelector<HTMLElement>("button, a, input")?.focus();
    }, 0);
  }

  function startNewConversation() {
    setDraft("");
    setActionError(null);
    setEditingId(null);
    navigate("/workspace");
    closeDrawer(true);
  }

  async function submitMessage() {
    const content = draft.trim();
    if (
      !content
      || conversationMutationPending
      || activeAttempt
      || archived
      || detailUnavailable
      || detailBlocked
      || !selectedAgent
      || sendLockRef.current
    ) return;
    sendLockRef.current = true;
    try {
      await sendMutation.mutateAsync({
        content,
        originConversationId: conversationId,
        sourceConversation: conversation,
        agent: selectedAgent
      });
    } catch {
      // Mutation callbacks render the API error and reconcile revision conflicts.
    } finally {
      sendLockRef.current = false;
    }
  }

  function submitComposer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void submitMessage();
  }

  function handleComposerKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void submitMessage();
  }

  function startRename(item: WorkspaceConversation) {
    setEditingId(item.id);
    setEditTitle(item.title);
  }

  function renameConversation(item: WorkspaceConversation) {
    const title = editTitle.trim();
    if (!title) return;
    const revision = item.id === conversation?.id ? conversation.revision : item.revision;
    updateMutation.mutate({
      id: item.id,
      revision,
      title,
      originConversationId: conversationId
    });
  }

  function archiveConversation(item: WorkspaceConversation, nextArchived: boolean) {
    const revision = item.id === conversation?.id ? conversation.revision : item.revision;
    updateMutation.mutate({
      id: item.id,
      revision,
      archived: nextArchived,
      originConversationId: conversationId
    });
  }

  function regenerateLatestTurn() {
    const latestTurn = conversation?.turns.at(-1);
    if (!conversation || !conversationId || !latestTurn || detailBlocked) return;
    regenerateMutation.mutate({
      id: conversation.id,
      userMessageId: latestTurn.user_message.id,
      revision: conversation.revision,
      originConversationId: conversation.id,
      sourceConversation: conversation
    });
  }

  function migrateConversationVersion() {
    if (!conversation || !migrationTarget) return;
    updateMutation.mutate({
      id: conversation.id,
      revision: conversation.revision,
      agentVersionId: migrationTarget.agent_version_id,
      originConversationId: conversation.id
    });
  }

  function handleLedgerScroll() {
    const ledger = ledgerRef.current;
    if (!ledger) return;
    pinnedToBottomRef.current = ledger.scrollHeight - ledger.scrollTop - ledger.clientHeight < 96;
  }

  const detailError = conversationQuery.error instanceof ApiError
    ? conversationQuery.error.message
    : conversationQuery.error instanceof Error
      ? conversationQuery.error.message
      : null;

  return (
    <main className="agent-workspace-page">
      <div
        className="conversation-drawer-backdrop"
        aria-hidden="true"
        hidden={!drawerOpen}
      />
      <aside
        ref={drawerRef}
        id="workspace-conversation-rail"
        className={`conversation-rail ${drawerOpen ? "is-open" : ""}`}
        aria-label="会话列表"
        aria-hidden={mobileDrawer && !drawerOpen ? true : undefined}
        aria-modal={mobileDrawer && drawerOpen ? true : undefined}
        role={mobileDrawer ? "dialog" : undefined}
        inert={mobileDrawer && !drawerOpen ? true : undefined}
      >
        <ConversationRail
          conversations={conversations}
          activeId={conversationId}
          loading={conversationsQuery.isPending}
          error={conversationsQuery.error?.message ?? null}
          editingId={editingId}
          editTitle={editTitle}
          mutationPending={updateMutation.isPending}
          onNew={startNewConversation}
          onClose={() => closeDrawer(true)}
          onStartRename={startRename}
          onEditTitle={setEditTitle}
          onRename={renameConversation}
          onArchive={archiveConversation}
        />
      </aside>

      <section className="workspace-console">
        <header className="workspace-instrument-bar">
          <button
            ref={drawerToggleRef}
            className="conversation-drawer-toggle"
            type="button"
            aria-label="打开会话列表"
            aria-expanded={drawerOpen}
            aria-controls="workspace-conversation-rail"
            onClick={openDrawer}
          >
            <span />
            <span />
            <span />
          </button>
          <div className="instrument-identity">
            <span className="instrument-index">{conversation ? `C-${conversation.id.slice(0, 6).toUpperCase()}` : "NEW SESSION"}</span>
            <div>
              <strong>{conversation?.title || "新建 Agent 会话"}</strong>
              <small>{conversation ? `REV ${String(conversation.revision).padStart(3, "0")}` : "等待建立服务端记录"}</small>
            </div>
          </div>
          <div className="instrument-agent">
            <span>AGENT CHANNEL</span>
            {conversation ? (
              <div className="instrument-agent-pinned">
                <strong>{conversation.agent.name} {conversation.agent.visibility === "private" && <b className="private-api-badge">私人 API</b>} <i>V{conversation.agent.version}</i>{conversation.agent.model && <small>{conversation.agent.model}</small>}</strong>
                {migrationTarget && (
                  <button
                    type="button"
                    disabled={updateMutation.isPending}
                    onClick={migrateConversationVersion}
                  >
                    迁移到当前版本 V{migrationTarget.version}
                  </button>
                )}
              </div>
            ) : (
              <AgentPicker
                agents={catalog}
                selectedVersionIds={selectedAgent ? [selectedAgent.agent_version_id] : []}
                recentVersionIds={conversations.map((item) => item.agent.agent_version_id)}
                mode="single"
                loading={catalogQuery.isPending}
                error={catalogQuery.error?.message ?? null}
                onChange={(versionIds) => setSelectedAgentVersionId(versionIds[0] ?? "")}
              />
            )}
          </div>
           <div className="instrument-status">
             <Link className="instrument-mode-link" to="/compare">并行对比</Link>
             {conversation && <div className="conversation-export-actions"><button type="button" onClick={() => downloadMarkdown(conversation.title, conversationMarkdown(conversation))}>MD</button><button type="button" onClick={() => downloadJson(conversation.title, conversation)}>JSON</button></div>}
             <i className={activeAttempt ? "is-active" : ""} />
            <span>{activeAttempt ? attemptLabel(activeAttempt) : "CHANNEL READY"}</span>
          </div>
        </header>

        <div className="message-ledger" ref={ledgerRef} onScroll={handleLedgerScroll}>
          {conversationQuery.isPending && conversationId && (
            <LoadingState label="正在装载消息账本" />
          )}
          {detailError && (
            <div className="workspace-detail-state is-error">
              <strong>无法读取此会话</strong><span>{detailError}</span>
              <button type="button" onClick={startNewConversation}>返回新对话</button>
            </div>
          )}
          {!conversationId && (
            <section className="workspace-empty-state">
              <div className="empty-grid-mark" aria-hidden="true"><span>01</span><span>02</span><span>03</span></div>
              <p>AGENT WORKSPACE / PERSISTENT CHANNEL</p>
              <h1>建立一条可追溯的<br />Agent 对话链路。</h1>
              <p className="empty-state-copy">
                选择已验证版本。消息、生成尝试、引用与 Token 指标将写入服务端账本。
              </p>
              {catalogQuery.isError && <div className="workspace-catalog-error">Agent 目录读取失败，请稍后重试。</div>}
              {selectedAgent && (
                <article className="selected-agent-card">
                  <span>{selectedAgent.name.slice(0, 2).toUpperCase()}</span>
                  <div>
                    <small>SELECTED CHANNEL</small>
                    <strong>{selectedAgent.name}{selectedAgent.visibility === "private" && <b className="private-api-badge">私人 API</b>}</strong>
                    <p>{selectedAgent.description || `${selectedAgent.provider} 提供的 Agent 服务`}</p>
                  </div>
                  <dl>
                    <div><dt>VERSION</dt><dd>V{selectedAgent.version}</dd></div>
                    {selectedAgent.model && <div><dt>MODEL</dt><dd>{selectedAgent.model}</dd></div>}
                    <div><dt>CITATIONS</dt><dd>{selectedAgent.supports_citations ? "YES" : "NO"}</dd></div>
                  </dl>
                  <button
                    type="button"
                    disabled={createMutation.isPending}
                    onClick={() => createMutation.mutate({ agent: selectedAgent, originConversationId: conversationId })}
                  >
                    {createMutation.isPending ? "正在建立" : "建立空会话"}
                  </button>
                </article>
              )}
              {!catalogQuery.isPending && catalog.length === 0 && !catalogQuery.isError && (
                <div className="workspace-catalog-error">当前账号没有可用的 Agent 版本。</div>
              )}
            </section>
          )}
          {conversation && conversation.turns.length === 0 && (
            <section className="workspace-conversation-empty">
              <span>LEDGER EMPTY / REV {String(conversation.revision).padStart(3, "0")}</span>
              <h2>会话已建立，等待第一条消息。</h2>
              <p>Agent 版本已锁定为 {conversation.agent.name}{conversation.agent.visibility === "private" ? " · 私人 API" : ""}{conversation.agent.model ? ` · ${conversation.agent.model}` : ""} V{conversation.agent.version}。</p>
            </section>
          )}
          {conversation?.turns.map((turn, index) => (
            <TurnLedger
              key={turn.turn_no}
              userId={userId}
              turn={turn}
              latestTurn={index === conversation.turns.length - 1}
               selectedVariant={variantSelection[turn.turn_no]}
               busy={conversationMutationPending}
               canDraft={canDraft && conversation.agent.visibility !== "private"}
               evaluationCaseIdsByAttempt={evaluationCaseIdsByAttempt}
               evaluationCasesReady={evaluationCasesQuery.isSuccess}
               draftingAttemptId={createDraftMutation.isPending ? createDraftMutation.variables?.attemptId : undefined}
               onSelectVariant={(variant) => setVariantSelection((current) => ({ ...current, [turn.turn_no]: variant }))}
              onCancel={(attemptId) => {
                 if (conversationId) cancelMutation.mutate({ attemptId, originConversationId: conversationId });
               }}
               onCreateDraft={(attemptId) => {
                 if (conversationId) createDraftMutation.mutate({ attemptId, originConversationId: conversationId });
               }}
               onRegenerate={regenerateLatestTurn}
            />
          ))}
          <div ref={ledgerEndRef} />
        </div>

        <form className="workspace-composer" onSubmit={submitComposer}>
          {actionError && <div className="composer-error" role="alert">{actionError}</div>}
          {archived && <div className="composer-notice">此会话已归档，恢复后才能继续发送。</div>}
          {conversation && !selectedAgent && (
            <div className="composer-notice">此会话固定的 Agent 版本当前不可用，请迁移到当前版本后继续。</div>
          )}
          <div className="composer-resources">
            <label><span>Prompt 模板</span><select aria-label="Prompt 模板" value="" disabled={templatesQuery.isPending || !templatesQuery.data?.items.length} onChange={(event) => {
              const template = templatesQuery.data?.items.find((item) => item.id === event.target.value);
              if (!template) return;
              if (draft || draftTouchedRef.current) {
                setActionError("为避免覆盖已输入内容，请先清空当前草稿再选择模板。");
                return;
              }
              draftTouchedRef.current = true;
              setDraft(template.content);
            }}><option value="">{templatesQuery.isPending ? "正在读取模板" : "选择模板填入空白草稿"}</option>{templatesQuery.data?.items.map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}</select></label>
            <Link to="/templates">管理模板</Link><Link to="/experience">体验任务</Link>
          </div>
          <div className="composer-grid">
            <span className="composer-prompt-mark" aria-hidden="true">›</span>
            <label className="sr-only" htmlFor="workspace-message">发送消息</label>
            <textarea
              id="workspace-message"
              ref={textareaRef}
              rows={1}
              value={draft}
              placeholder={selectedAgent ? `向 ${selectedAgent.name} 输入消息` : "请先选择可用 Agent"}
              disabled={
                !selectedAgent
                || catalogQuery.isPending
                || archived
                || detailUnavailable
                || detailBlocked
                || conversationMutationPending
              }
              onChange={(event) => { draftTouchedRef.current = true; setDraft(event.target.value); }}
              onKeyDown={handleComposerKeyDown}
            />
            <button
              type="submit"
              disabled={
                !draft.trim()
                || conversationMutationPending
                || Boolean(activeAttempt)
                || archived
                || detailUnavailable
                || detailBlocked
                || !selectedAgent
              }
            >
              {sendMutation.isPending && sendMutation.variables?.originConversationId === conversationId
                ? "发送中"
                : activeAttempt ? "生成中" : "发送"}
              <span aria-hidden="true">↗</span>
            </button>
          </div>
          <footer>
            <span>ENTER 发送 · SHIFT+ENTER 换行</span>
            <span aria-live="polite">
              本轮 {draft.length} 字{maximumCharacters == null ? "" : ` · Agent 上限 ${maximumCharacters}`} · 完整上下文由服务器校验
            </span>
          </footer>
        </form>
      </section>
    </main>
  );
}
