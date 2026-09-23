import { expect, test } from "@playwright/test";

import { loginAsRole } from "./helpers";

test.describe("评测运行", () => {
  test("发起评测表单流程到配置校验提示", async ({ page }) => {
    await loginAsRole(page, "operator");
    await page.getByRole("link", { name: "评测运行" }).click();
    await expect(page).toHaveURL(/\/runs$/);

    await page.getByRole("button", { name: "+ 发起评测" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText("发起基准评测")).toBeVisible();

    // The disposable E2E database has a fake Agent but no frozen datasets or
    // judge/score configs, so the form must surface the readiness issues
    // instead of letting an invalid run through.
    await expect(dialog.getByText("没有可用的冻结题库")).toBeVisible();
    await expect(dialog.getByText("没有可用的裁判配置")).toBeVisible();
  });
});
