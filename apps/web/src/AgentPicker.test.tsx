import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, test } from "vitest";

import { AgentPicker } from "./AgentPicker";
import { WorkspaceAgent } from "./api";

afterEach(cleanup);

const agents: WorkspaceAgent[] = [
  {
    agent_id: "agent-1",
    agent_version_id: "version-1",
    name: "Alpha Agent",
    provider: "Alpha Labs",
    description: "Research model",
    logo_url: null,
    version: 2,
    max_prompt_characters: 4_000,
    supports_citations: true,
    visibility: "public",
    model: null
  },
  {
    agent_id: "agent-2",
    agent_version_id: "version-2",
    name: "Beta Agent",
    provider: "Beta Cloud",
    description: "Writing model",
    logo_url: null,
    version: 4,
    max_prompt_characters: null,
    supports_citations: false,
    visibility: "public",
    model: null
  },
  {
    agent_id: "private-1",
    agent_version_id: "private-version-1",
    name: "My Qwen",
    provider: "阿里百炼",
    description: "Private research model",
    logo_url: null,
    version: 1,
    max_prompt_characters: null,
    supports_citations: false,
    visibility: "private",
    model: "qwen-max-latest"
  }
];

function PickerHarness() {
  const [selected, setSelected] = useState<string[]>([]);
  return (
    <AgentPicker
      agents={agents}
      selectedVersionIds={selected}
      recentVersionIds={["version-2"]}
      mode="multiple"
      onChange={setSelected}
    />
  );
}

test("searches and filters a provider-grouped Agent directory with server-derived recents", () => {
  render(<PickerHarness />);
  fireEvent.click(screen.getByRole("button", { name: "选择对比 Agent" }));

  const dialog = screen.getByRole("dialog", { name: "Agent 目录" });
  expect(dialog.parentElement?.parentElement).toBe(document.body);
  expect(document.body).toHaveStyle({ overflow: "hidden" });
  expect(within(dialog).getAllByRole("heading")[0]).toHaveTextContent("我的 API");
  expect(within(dialog).getByText("最近使用")).toBeInTheDocument();
  expect(within(dialog).getByRole("heading", { name: /Alpha Labs/ })).toBeInTheDocument();

  fireEvent.change(within(dialog).getByRole("searchbox", { name: "搜索 Agent" }), {
    target: { value: "research" }
  });
  expect(within(dialog).getByRole("checkbox", { name: /Alpha Agent/ })).toBeInTheDocument();
  expect(within(dialog).queryByRole("checkbox", { name: /Beta Agent/ })).not.toBeInTheDocument();

  fireEvent.change(within(dialog).getByRole("searchbox", { name: "搜索 Agent" }), {
    target: { value: "" }
  });
  fireEvent.change(within(dialog).getByRole("combobox", { name: "筛选提供方" }), {
    target: { value: "Beta Cloud" }
  });
  fireEvent.click(within(dialog).getByRole("checkbox", { name: /Beta Agent/ }));
  expect(screen.getByRole("button", { name: "移除 Beta Agent" })).toBeInTheDocument();
  expect(within(dialog).queryByRole("checkbox", { name: /Alpha Agent/ })).not.toBeInTheDocument();

  const backdrop = dialog.parentElement!;
  fireEvent.mouseDown(backdrop);
  fireEvent.click(backdrop);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.getByRole("dialog", { name: "Agent 目录" })).toBeInTheDocument();

  fireEvent.click(within(dialog).getByRole("button", { name: "关闭 Agent 目录" }));
  expect(document.body).not.toHaveStyle({ overflow: "hidden" });
});

test("searches private Agents by model and shows private identity in options and selected chips", () => {
  render(<PickerHarness />);
  fireEvent.click(screen.getByRole("button", { name: "选择对比 Agent" }));

  const dialog = screen.getByRole("dialog", { name: "Agent 目录" });
  fireEvent.change(within(dialog).getByRole("searchbox", { name: "搜索 Agent" }), {
    target: { value: "qwen-max-latest" }
  });
  const privateOption = within(dialog).getByRole("checkbox", { name: /My Qwen.*qwen-max-latest.*私人 API/ });
  expect(privateOption.closest("label")).toHaveTextContent("私人 API");
  expect(privateOption.closest("label")).toHaveTextContent("qwen-max-latest");
  fireEvent.click(privateOption);

  const selection = screen.getByLabelText("已选择 Agent");
  expect(selection).toHaveTextContent("My Qwen");
  expect(selection).toHaveTextContent("私人 API");
  expect(selection).toHaveTextContent("qwen-max-latest");
});

test("shows private and model identity in a single-selection trigger", () => {
  render(
    <AgentPicker
      agents={agents}
      selectedVersionIds={["private-version-1"]}
      mode="single"
      onChange={() => undefined}
    />
  );

  const trigger = screen.getByRole("button", { name: "选择 Agent" });
  expect(trigger).toHaveTextContent("My Qwen");
  expect(trigger).toHaveTextContent("私人 API");
  expect(trigger).toHaveTextContent("qwen-max-latest");
});
