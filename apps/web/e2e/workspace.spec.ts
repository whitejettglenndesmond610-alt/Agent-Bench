import { expect, test } from "@playwright/test";

import { loginAsRole } from "./helpers";

test.describe("在线对话", () => {
  test("发消息并收到 fake Agent 回答", async ({ page }) => {
    await loginAsRole(page, "read_only");
    await page.getByRole("link", { name: "在线对话", exact: true }).click();
    await expect(page).toHaveURL(/\/workspace$/);

    const composer = page.locator("#workspace-message");
    await expect(composer).toBeEnabled();
    await composer.fill("你好，请介绍一下自己");
    await page.getByRole("button", { name: "发送", exact: true }).click();

    // The worker picks up the queued attempt and calls the local fake Agent.
    await expect(page.getByText("Fake Agent Alpha deterministic answer")).toBeVisible({
      timeout: 30_000
    });
  });
});
