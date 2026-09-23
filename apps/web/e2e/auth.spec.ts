import { expect, test } from "@playwright/test";

import { loginAsRole } from "./helpers";

test.describe("认证", () => {
  test("登录页渲染，错误密码报错", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "选择访问身份" })).toBeVisible();

    await page.getByLabel("共享密码").fill("wrong-password");
    await page.getByRole("button", { name: "进入平台" }).click();
    await expect(page.getByRole("alert")).toContainText("角色或密码错误");
  });

  test("正确密码进入首页", async ({ page }) => {
    await loginAsRole(page, "read_only");
    await expect(page).toHaveURL(/\/home$/);
    await expect(page.getByRole("link", { name: "在线对话", exact: true })).toBeVisible();
  });

  test("未登录访问受保护页面回到登录页", async ({ page }) => {
    await page.goto("/leaderboard");
    await expect(page.getByRole("heading", { name: "选择访问身份" })).toBeVisible();
  });
});
