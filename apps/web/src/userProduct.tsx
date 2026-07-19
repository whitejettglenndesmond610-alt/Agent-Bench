/* eslint-disable react-refresh/only-export-components */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ButtonHTMLAttributes, useEffect, useState } from "react";

import {
  LeaderboardSnapshot,
  setWorkspaceAgentFavorite,
  updateWorkspaceAttemptAnnotation,
  WorkspaceAgent,
  WorkspaceAttempt,
  WorkspaceComparison,
  WorkspaceConversationDetail
} from "./api";

export const userQueryKeys = {
  agents: (userId: string) => ["workspace", userId, "agents"] as const,
  agentOptions: (userId: string) => ["workspace", userId, "agent-options"] as const,
  privateAgents: (userId: string) => ["workspace", userId, "private-agents"] as const,
  conversations: (userId: string) => ["workspace", userId, "conversations"] as const,
  conversation: (userId: string, id: string) => ["workspace", userId, "conversation", id] as const,
  comparisons: (userId: string) => ["workspace", userId, "comparisons"] as const,
  comparison: (userId: string, id: string) => ["workspace", userId, "comparison", id] as const,
  templates: (userId: string) => ["workspace", userId, "prompt-templates"] as const,
  template: (userId: string, id: string) => ["workspace", userId, "prompt-template", id] as const,
  tasks: (userId: string) => ["workspace", userId, "experience-tasks"] as const,
  task: (userId: string, slug: string) => ["workspace", userId, "experience-task", slug] as const
};

export function formatUserDate(value: string | null | undefined, includeYear = false): string {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    ...(includeYear ? { year: "numeric" } : {}),
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(date);
}

export async function writeClipboard(text: string): Promise<void> {
  if (!navigator.clipboard?.writeText) throw new Error("当前浏览器不支持剪贴板写入");
  await navigator.clipboard.writeText(text);
}

export function ClipboardButton({
  text,
  children,
  successLabel = "已复制",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  text: string;
  successLabel?: string;
}) {
  const [state, setState] = useState<"idle" | "success" | "error">("idle");

  async function copy() {
    try {
      await writeClipboard(text);
      setState("success");
    } catch {
      setState("error");
    }
  }

  return (
    <button type="button" {...props} onClick={copy}>
      {state === "success" ? successLabel : state === "error" ? "复制失败" : children}
      <span className="sr-only" aria-live="polite">
        {state === "success" ? successLabel : state === "error" ? "复制失败，请手动选择文本" : ""}
      </span>
    </button>
  );
}

function safeFilename(value: string): string {
  const printable = Array.from(value, (character) => character.charCodeAt(0) < 32 ? "-" : character).join("");
  const normalized = printable.trim().replace(/[\\/:*?"<>|]+/g, "-").slice(0, 80);
  return normalized || "agent-bench-export";
}

export function downloadText(filename: string, content: string, type: string): void {
  const blob = new Blob([content], { type });
  const createObjectUrl = URL.createObjectURL;
  if (!createObjectUrl) return;
  const url = createObjectUrl(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = safeFilename(filename);
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function markdownCitations(citations: string[]): string {
  return citations.length
    ? `\n\n引用：\n${citations.map((citation, index) => `${index + 1}. ${citation}`).join("\n")}`
    : "";
}

export function conversationMarkdown(conversation: WorkspaceConversationDetail): string {
  const turns = conversation.turns.map((turn) => {
    const answer = turn.assistant_messages.find((message) => message.active)
      ?? turn.assistant_messages.at(-1);
    return [
      `## 第 ${turn.turn_no} 轮`,
      `### 提问\n${turn.user_message.content}`,
      answer
        ? `### ${conversation.agent.name}\n${answer.content}${markdownCitations(answer.citations ?? [])}`
        : "### 回答\n（尚无成功回答）"
    ].join("\n\n");
  });
  return `# ${conversation.title}\n\nAgent: ${conversation.agent.name} V${conversation.agent.version}\n\n${turns.join("\n\n---\n\n")}\n`;
}

export function comparisonMarkdown(comparison: WorkspaceComparison): string {
  const answers = [...comparison.items]
    .sort((left, right) => left.position - right.position)
    .map((item) => {
      const answer = item.attempt.assistant_message;
      return `## ${item.agent.name} V${item.agent.version}\n\n${answer?.content ?? `（${item.attempt.status}）`}${markdownCitations(answer?.citations ?? [])}`;
    });
  return `# ${comparison.title}\n\n## 统一提示词\n\n${comparison.prompt}\n\n${answers.join("\n\n---\n\n")}\n`;
}

export function snapshotMarkdown(snapshot: LeaderboardSnapshot): string {
  const header = "| 排名 | Agent | 版本 | 综合分 | 成功率 |\n| ---: | --- | ---: | ---: | ---: |";
  const rows = snapshot.rankings.map((ranking) =>
    `| ${ranking.rank} | ${ranking.agent_name} | V${ranking.version} | ${ranking.overall_score.toFixed(2)} | ${ranking.success_rate.toFixed(2)}% |`
  );
  return `# ${snapshot.run_name}\n\n发布时间：${formatUserDate(snapshot.published_at, true)}\n\n${header}\n${rows.join("\n")}\n`;
}

export function downloadJson(filename: string, value: unknown): void {
  downloadText(`${filename}.json`, `${JSON.stringify(value, null, 2)}\n`, "application/json;charset=utf-8");
}

export function downloadMarkdown(filename: string, value: string): void {
  downloadText(`${filename}.md`, value, "text/markdown;charset=utf-8");
}

function replaceAttempt(value: unknown, updated: WorkspaceAttempt): unknown {
  if (Array.isArray(value)) return value.map((item) => replaceAttempt(item, updated));
  if (!value || typeof value !== "object") return value;
  const record = value as Record<string, unknown>;
  if (record.id === updated.id && typeof record.status === "string" && "agent_version_id" in record) {
    return { ...record, ...updated };
  }
  return Object.fromEntries(Object.entries(record).map(([key, item]) => [key, replaceAttempt(item, updated)]));
}

export function AttemptAnnotationControls({
  userId,
  attempt,
  compact = false
}: {
  userId: string;
  attempt: WorkspaceAttempt;
  compact?: boolean;
}) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState(attempt.private_note ?? "");
  const [message, setMessage] = useState("");

  useEffect(() => setNote(attempt.private_note ?? ""), [attempt.id, attempt.private_note]);

  const mutation = useMutation({
    mutationFn: (input: Parameters<typeof updateWorkspaceAttemptAnnotation>[1]) =>
      updateWorkspaceAttemptAnnotation(attempt.id, input),
    onSuccess: (updated) => {
      queryClient.setQueriesData({ queryKey: ["workspace", userId] }, (current) =>
        replaceAttempt(current, updated)
      );
      setNote(updated.private_note ?? "");
      setMessage("标注已保存");
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : "标注保存失败")
  });

  return (
    <div className={`attempt-annotation ${compact ? "is-compact" : ""}`}>
      <div className="annotation-actions" aria-label="回答标注">
        <button
          type="button"
          aria-label="回答有帮助"
          aria-pressed={attempt.feedback === "up"}
          disabled={mutation.isPending}
          onClick={() => mutation.mutate({ feedback: attempt.feedback === "up" ? null : "up" })}
        >赞</button>
        <button
          type="button"
          aria-label="回答没帮助"
          aria-pressed={attempt.feedback === "down"}
          disabled={mutation.isPending}
          onClick={() => mutation.mutate({ feedback: attempt.feedback === "down" ? null : "down" })}
        >踩</button>
        <button
          type="button"
          aria-label={attempt.bookmarked ? "取消收藏回答" : "收藏回答"}
          aria-pressed={Boolean(attempt.bookmarked)}
          disabled={mutation.isPending}
          onClick={() => mutation.mutate({ bookmarked: !attempt.bookmarked })}
        >{attempt.bookmarked ? "已收藏" : "收藏"}</button>
      </div>
      <details className="annotation-note" open={Boolean(attempt.private_note) && !compact}>
        <summary>{attempt.private_note ? "编辑私人笔记" : "添加私人笔记"}</summary>
        <label>
          <span className="sr-only">私人笔记</span>
          <textarea
            aria-label="私人笔记"
            maxLength={10_000}
            rows={3}
            value={note}
            placeholder="仅你可见，不会发送给 Agent"
            onChange={(event) => setNote(event.target.value)}
          />
        </label>
        <button
          type="button"
          disabled={mutation.isPending || note.trim() === (attempt.private_note ?? "")}
          onClick={() => mutation.mutate({ private_note: note.trim() || null })}
        >{mutation.isPending ? "保存中" : "保存笔记"}</button>
      </details>
      <span className="annotation-status" role="status" aria-live="polite">{message}</span>
    </div>
  );
}

export function FavoriteAgentButton({
  userId,
  agent,
  className = ""
}: {
  userId: string;
  agent: WorkspaceAgent;
  className?: string;
}) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => setWorkspaceAgentFavorite(agent.agent_id, !agent.is_favorite),
    onSuccess: () => {
      queryClient.setQueryData<WorkspaceAgent[]>(userQueryKeys.agents(userId), (current) =>
        current?.map((item) => item.agent_id === agent.agent_id
          ? { ...item, is_favorite: !agent.is_favorite }
          : item)
      );
    }
  });

  return (
    <button
      type="button"
      className={className}
      aria-label={agent.is_favorite ? `取消收藏 ${agent.name}` : `收藏 ${agent.name}`}
      aria-pressed={Boolean(agent.is_favorite)}
      disabled={mutation.isPending}
      title={mutation.error instanceof Error ? mutation.error.message : undefined}
      onClick={() => mutation.mutate()}
    >{agent.is_favorite ? "★ 已收藏" : "☆ 收藏"}</button>
  );
}
