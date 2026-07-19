import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, test } from "vitest";

import { Modal } from "./components";

afterEach(cleanup);

function ModalHarness() {
  const [open, setOpen] = useState(true);
  return open ? (
    <Modal title="测试弹窗" onClose={() => setOpen(false)}>
      <p>不会误触关闭</p>
    </Modal>
  ) : null;
}

test("keeps Modal open on backdrop clicks and Escape until explicitly closed", () => {
  const { container } = render(<ModalHarness />);
  const backdrop = container.querySelector(".modal-backdrop");
  expect(backdrop).not.toBeNull();

  fireEvent.mouseDown(backdrop!);
  fireEvent.click(backdrop!);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.getByRole("dialog", { name: "测试弹窗" })).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  expect(screen.queryByRole("dialog", { name: "测试弹窗" })).not.toBeInTheDocument();
});
