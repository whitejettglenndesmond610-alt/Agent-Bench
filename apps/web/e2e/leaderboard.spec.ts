import { expect, test } from "@playwright/test";

import { loginAsRole } from "./helpers";

test.describe("排行榜", () => {
  test("加载排行榜页（空库空态）", async ({ page }) => {
    await loginAsRole(page, "read_only");
    await page.getByRole("link", { name: "排行榜" }).click();
    await expect(page).toHaveURL(/\/leaderboard$/);
    await expect(page.getByRole("heading", { name: "Agent 排行榜" })).toBeVisible();
    // Empty database: podium slots stay unoccupied and the seeded fake Agent
    // shows up in the unranked section.
    await expect(page.getByText("暂无 Agent").first()).toBeVisible();
    await expect(page.getByText("尚未取得任何正式成绩的 Agent")).toBeVisible();
  });
});
