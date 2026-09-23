import { expect, type Page } from "@playwright/test";

import { passwords } from "./env";

/** Log in through the role-password form and wait for the app shell. */
export async function loginAsRole(page: Page, role: "read_only" | "operator") {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "选择访问身份" })).toBeVisible();
  if (role === "operator") {
    await page.getByRole("button", { name: /评测管理员/ }).click();
  }
  await page
    .getByLabel("共享密码")
    .fill(role === "operator" ? passwords.operator : passwords.readOnly);
  await page.getByRole("button", { name: "进入平台" }).click();
  await expect(page.getByRole("link", { name: "排行榜" })).toBeVisible();
}
