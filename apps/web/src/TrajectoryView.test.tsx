import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { RunTrajectory } from "./api";
import { TrajectoryView } from "./TrajectoryView";

afterEach(cleanup);

const baseTrajectory: RunTrajectory = {
  tau2_version: "1.0.1",
  domain: "retail",
  task_id: "task-1",
  task_split: "base",
  trial_index: 0,
  agent_protocol: "http-text-json-v1",
  simulator_model: "deepseek/deepseek-chat",
  turns: 4,
  termination_reason: "user_stop",
  agent_protocol_error: null,
  reward_info: {
    reward: 1.0,
    db_check: { db_match: true, db_reward: 1.0 },
    action_checks: [],
    nl_assertions: [],
    communicate_checks: [],
    reward_basis: ["DB"]
  },
  messages: [
    { role: "assistant", content: "Hi! How can I help you today?" },
    { role: "user", content: "Please **cancel** my order." },
    {
      role: "assistant",
      content: null,
      tool_calls: [{ id: "call-1", name: "cancel_order", arguments: { order_id: "o-123" }, requestor: "assistant" }]
    },
    { role: "tool", id: "call-1", content: "order o-123 cancelled", requestor: "assistant", error: false },
    { role: "assistant", content: "Your order is cancelled." },
    { role: "user", content: "###STOP###" }
  ]
};

test("renders header stats and chat flow with collapsible tool cards", () => {
  const rendered = render(<TrajectoryView trajectory={baseTrajectory} />);

  expect(screen.getByRole("heading", { name: "交互轨迹回放" })).toBeInTheDocument();
  expect(screen.getByText("1.00")).toBeInTheDocument();
  expect(screen.getByText("用户模拟器结束")).toBeInTheDocument();
  expect(screen.getByText("http-text-json-v1")).toBeInTheDocument();
  expect(screen.getByText("deepseek/deepseek-chat")).toBeInTheDocument();
  expect(screen.getByText("retail / task-1 · 试次 0")).toBeInTheDocument();

  // user messages render as plain text (markdown markers stay literal),
  // assistant messages go through the markdown renderer
  expect(screen.getByText("Please **cancel** my order.")).toBeInTheDocument();
  expect(screen.getByText("Your order is cancelled.")).toBeInTheDocument();

  // tool call and tool result are collapsed cards
  const toolCall = screen.getByText("cancel_order").closest("details");
  expect(toolCall).not.toBeNull();
  expect(toolCall?.open).toBe(false);
  expect(rendered.container.textContent).toContain('"order_id": "o-123"');
  const toolResult = screen.getByText("返回内容").closest("details");
  expect(toolResult?.open).toBe(false);
  expect(screen.getByText("order o-123 cancelled")).toBeInTheDocument();

  // all assertions passed -> no failure list
  expect(rendered.container.querySelector(".trajectory-failures")).toBeNull();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("lists failed assertions and protocol errors", () => {
  const trajectory: RunTrajectory = {
    ...baseTrajectory,
    termination_reason: "max_steps",
    agent_protocol_error: "reply contains no fenced ```json block",
    reward_info: {
      reward: 0.0,
      db_check: { db_match: false, db_reward: 0.0 },
      action_checks: [
        { action: { name: "cancel_order", arguments: { order_id: "o-123" } }, action_match: false, action_reward: 0.0 }
      ],
      nl_assertions: [
        { nl_assertion: "Agent confirms cancellation", met: false, justification: "never stated" }
      ],
      communicate_checks: [
        { info: "cancellation confirmation", met: false, justification: "missing" }
      ]
    },
    messages: []
  };
  render(<TrajectoryView trajectory={trajectory} />);

  expect(screen.getByText("达到最大步数")).toBeInTheDocument();
  expect(screen.getByRole("alert").textContent).toContain("reply contains no fenced");
  expect(screen.getByText("未通过的评估断言")).toBeInTheDocument();
  expect(screen.getByText(/数据库终态不匹配/)).toBeInTheDocument();
  expect(screen.getByText(/动作未执行/)).toBeInTheDocument();
  expect(screen.getByText(/NL 断言未满足：Agent confirms cancellation/)).toBeInTheDocument();
  expect(screen.getByText(/未向用户传达：cancellation confirmation/)).toBeInTheDocument();
});

test("flattens multi-tool messages and marks tool errors", () => {
  const trajectory: RunTrajectory = {
    ...baseTrajectory,
    reward_info: null,
    messages: [
      {
        role: "tool",
        tool_messages: [
          { role: "tool", id: "call-1", content: "ok", requestor: "assistant", error: false },
          { role: "tool", id: "call-2", content: "boom", requestor: "assistant", error: true }
        ]
      }
    ]
  };
  const rendered = render(<TrajectoryView trajectory={trajectory} />);

  expect(screen.getByText("ok")).toBeInTheDocument();
  expect(screen.getByText("boom")).toBeInTheDocument();
  expect(rendered.container.querySelectorAll(".trajectory-tool-card.is-error")).toHaveLength(1);
  // reward absent -> no reward figure in the header
  expect(rendered.container.querySelector(".trajectory-header strong")).toBeNull();
});
