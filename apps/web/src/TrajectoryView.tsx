import { ReactNode } from "react";

import { RunTrajectory, TrajectoryMessage, TrajectoryToolCall } from "./api";
import { MarkdownContent } from "./markdown";

const terminationLabels: Record<string, string> = {
  agent_stop: "Agent 主动结束",
  user_stop: "用户模拟器结束",
  max_steps: "达到最大步数",
  timeout: "超时",
  too_many_errors: "错误过多",
  agent_error: "Agent 错误",
  user_error: "用户模拟器错误",
  infrastructure_error: "基础设施错误",
  context_window_exceeded: "上下文超限",
  unexpected_error: "未知错误"
};

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function ToolCallCard({ call }: { call: TrajectoryToolCall }) {
  return (
    <details className="trajectory-tool-card">
      <summary>
        <span className="trajectory-tool-kind">工具调用</span>
        <strong>{call.name}</strong>
      </summary>
      <pre>{formatJson(call.arguments)}</pre>
    </details>
  );
}

function ToolResultCard({ message }: { message: TrajectoryMessage }) {
  return (
    <details className={`trajectory-tool-card is-result${message.error ? " is-error" : ""}`}>
      <summary>
        <span className="trajectory-tool-kind">工具结果</span>
        <strong>{message.error ? "执行失败" : "返回内容"}</strong>
      </summary>
      <pre>{message.content ?? "(空)"}</pre>
    </details>
  );
}

function TrajectoryMessageView({ message }: { message: TrajectoryMessage }) {
  if (message.role === "tool" && message.tool_messages) {
    return (
      <>
        {message.tool_messages.map((toolMessage, index) => (
          <TrajectoryMessageView message={toolMessage} key={toolMessage.id ?? index} />
        ))}
      </>
    );
  }
  if (message.role === "tool") {
    return (
      <div className="trajectory-message is-tool">
        <ToolResultCard message={message} />
      </div>
    );
  }
  const isUser = message.role === "user";
  const toolCalls = message.tool_calls ?? [];
  return (
    <div className={`trajectory-message ${isUser ? "is-user" : "is-assistant"}`}>
      <span className="trajectory-speaker">{isUser ? "用户模拟器" : "Agent"}</span>
      {message.content && (
        <div className="trajectory-bubble">
          {isUser ? message.content : <MarkdownContent content={message.content} />}
        </div>
      )}
      {toolCalls.map((call) => <ToolCallCard call={call} key={call.id} />)}
    </div>
  );
}

function FailedAssertions({ trajectory }: { trajectory: RunTrajectory }) {
  const rewardInfo = trajectory.reward_info;
  const failures: ReactNode[] = [];
  if (rewardInfo?.db_check && !rewardInfo.db_check.db_match) {
    failures.push(
      <li key="db">
        数据库终态不匹配<small>db_reward = {rewardInfo.db_check.db_reward}</small>
      </li>
    );
  }
  for (const check of rewardInfo?.action_checks ?? []) {
    if (!check.action_match) {
      failures.push(
        <li key={`action-${check.action.name}-${failures.length}`}>
          动作未执行：<code>{check.action.name}</code>
          <small>{formatJson(check.action.arguments)}</small>
        </li>
      );
    }
  }
  for (const assertion of rewardInfo?.nl_assertions ?? []) {
    if (!assertion.met) {
      failures.push(
        <li key={`nl-${failures.length}`}>
          NL 断言未满足：{assertion.nl_assertion}
          <small>{assertion.justification}</small>
        </li>
      );
    }
  }
  for (const check of rewardInfo?.communicate_checks ?? []) {
    if (!check.met) {
      failures.push(
        <li key={`communicate-${failures.length}`}>
          未向用户传达：{check.info}
          <small>{check.justification}</small>
        </li>
      );
    }
  }
  if (!failures.length) return null;
  return (
    <div className="trajectory-failures">
      <strong>未通过的评估断言</strong>
      <ul>{failures}</ul>
    </div>
  );
}

export function TrajectoryView({ trajectory }: { trajectory: RunTrajectory }) {
  const reward = trajectory.reward_info?.reward;
  return (
    <section className="trajectory-panel">
      <header className="trajectory-header">
        <h2>交互轨迹回放</h2>
        {reward !== undefined && reward !== null && (
          <strong className={reward >= 1 ? "is-success" : ""}>{reward.toFixed(2)}</strong>
        )}
      </header>
      <div className="trajectory-stats">
        <div><span>终止原因</span><strong>{terminationLabels[trajectory.termination_reason] ?? trajectory.termination_reason}</strong></div>
        <div><span>轮次</span><strong>{trajectory.turns}</strong></div>
        <div><span>协议</span><strong>{trajectory.agent_protocol}</strong></div>
        <div><span>模拟器</span><strong>{trajectory.simulator_model}</strong></div>
        <div><span>任务</span><strong>{trajectory.domain} / {trajectory.task_id} · 试次 {trajectory.trial_index}</strong></div>
      </div>
      {trajectory.agent_protocol_error && (
        <div className="result-banner is-error" role="alert">
          Agent 协议错误：{trajectory.agent_protocol_error}
        </div>
      )}
      <FailedAssertions trajectory={trajectory} />
      <div className="trajectory-messages">
        {trajectory.messages.map((message, index) => (
          <TrajectoryMessageView message={message} key={index} />
        ))}
      </div>
    </section>
  );
}
