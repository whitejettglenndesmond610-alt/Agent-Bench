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
    supports_citations: true
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
    supports_citations: false
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

test("searches and filters a provider-grouped catalog with server-derived recents", () => {
  render(<PickerHarness />);
  fireEvent.click(screen.getByRole("button", { name: "选择对比 Agent" }));

  const dialog = screen.getByRole("dialog", { name: "Agent 模型目录" });
  expect(within(dialog).getByText("最近使用")).toBeInTheDocument();
  expect(within(dialog).getByRole("heading", { name: /Alpha Labs/ })).toBeInTheDocument();

  fireEvent.change(within(dialog).getByRole("searchbox", { name: "搜索模型" }), {
    target: { value: "research" }
  });
  expect(within(dialog).getByRole("checkbox", { name: /Alpha Agent/ })).toBeInTheDocument();
  expect(within(dialog).queryByRole("checkbox", { name: /Beta Agent/ })).not.toBeInTheDocument();

  fireEvent.change(within(dialog).getByRole("searchbox", { name: "搜索模型" }), {
    target: { value: "" }
  });
  fireEvent.change(within(dialog).getByRole("combobox", { name: "筛选厂商" }), {
    target: { value: "Beta Cloud" }
  });
  fireEvent.click(within(dialog).getByRole("checkbox", { name: /Beta Agent/ }));
  expect(screen.getByRole("button", { name: "移除 Beta Agent" })).toBeInTheDocument();
  expect(within(dialog).queryByRole("checkbox", { name: /Alpha Agent/ })).not.toBeInTheDocument();
});
