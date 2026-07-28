import { expect, type Page, test } from "@playwright/test";

const teacherCredentials = { username: "teacher", password: "Teacher123!" };
const studentCredentials = { username: "student1", password: "Student123!" };

async function login(page: Page, credentials: typeof teacherCredentials) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill(credentials.username);
  await page.getByRole("textbox", { name: "密码", exact: true }).fill(credentials.password);
  await page.getByRole("button", { name: "登录并进入工作台" }).click();
}

test("teacher publishes, student submits, Agent evaluates, and teacher confirms", async ({ browser }) => {
  const title = "验收：Dijkstra 最短路";
  const teacher = await browser.newContext();
  const teacherPage = await teacher.newPage();
  await login(teacherPage, teacherCredentials);
  await expect(teacherPage.getByRole("heading", { name: "教师评阅工作台" })).toBeVisible();

  await teacherPage.getByRole("button", { name: "发布新作业" }).click();
  await teacherPage.getByLabel("作业标题").fill(title);
  await teacherPage.getByLabel("题目内容").fill("说明算法、复杂度与适用条件。");
  await teacherPage.getByLabel("学生说明").fill("请给出关键松弛步骤。");
  await teacherPage.getByLabel("提交截止时间").fill("2098-12-31T23:59");
  await teacherPage.getByLabel("新增评分要点").fill("说明非负权限制");
  await teacherPage.getByRole("button", { name: "添加要点" }).click();
  await teacherPage.getByLabel("新增评分要点").fill("分析优先队列复杂度");
  await teacherPage.getByRole("button", { name: "添加要点" }).click();
  await teacherPage.getByLabel("教师评分注意事项").fill("以概念正确和边界完整为准。");
  await teacherPage.getByRole("button", { name: "保存作业草稿" }).click();

  const assignmentCard = teacherPage.locator("article.assignment-record").filter({ hasText: title });
  await expect(assignmentCard).toBeVisible();
  const cardText = await assignmentCard.textContent();
  const code = cardText?.match(/HW-\d{4,10}/u)?.[0];
  expect(code).toMatch(/^HW-\d{4,10}$/u);
  await assignmentCard.getByRole("button", { name: `发布 ${code}` }).click();
  await expect(assignmentCard).toContainText("已发布");

  const student = await browser.newContext();
  const studentPage = await student.newPage();
  await login(studentPage, studentCredentials);
  await expect(studentPage.getByRole("heading", { name: "我的作业" })).toBeVisible();
  await studentPage.getByRole("link", { name: `打开 ${code}` }).click();
  await studentPage.getByLabel("作业答案").fill(
    "Dijkstra 使用邻接表和优先队列，每次取出未确定的最短距离顶点并松弛边。它只适用于非负权边，复杂度为 O((V+E)logV)。",
  );
  await studentPage.getByRole("button", { name: "提交答案" }).click();
  await expect(studentPage.getByText("答案已提交为第 1 版。")).toBeVisible();

  await teacherPage.getByRole("link", { name: title }).click();
  await expect(teacherPage.getByRole("heading", { name: title })).toBeVisible();
  await teacherPage.getByRole("button", { name: "评估全部最新提交" }).click();
  const studentRow = teacherPage.getByRole("row").filter({ hasText: "学生甲" });
  await expect(studentRow).toContainText("待审核");
  await studentRow.getByRole("link", { name: "打开报告" }).click();
  await expect(teacherPage.getByRole("heading", { name: "评估报告审阅" })).toBeVisible();
  await expect(teacherPage.getByLabel("评分摘要")).toContainText("72 · C");
  await teacherPage.getByLabel("审核评语（可选）").fill("验收确认：报告字段完整。");
  await teacherPage.getByRole("button", { name: "确认评估" }).click();
  await expect(teacherPage.getByText("评估结果已确认。")).toBeVisible();
  await expect(teacherPage.getByLabel("评估边界")).toContainText("confirmed");

  await teacher.close();
  await student.close();
});

test("teacher modification and re-evaluation preserve report history", async ({ browser }) => {
  const teacher = await browser.newContext();
  const page = await teacher.newPage();
  await login(page, teacherCredentials);
  await page.getByRole("link", { name: "图的最短路径" }).click();

  const studentRow = page.getByRole("row").filter({ hasText: "学生甲" });
  await studentRow.getByRole("link", { name: "打开报告" }).click();
  await expect(page.getByText("REPORT REVIEW / v1")).toBeVisible();
  await page.getByRole("button", { name: "修改评估" }).click();
  await page.getByLabel("评分", { exact: true }).fill("88");
  await page.getByLabel("审核评语（大幅改分必填）").fill("教师复核后调整分数，用于验证修订留痕。");
  await page.getByRole("button", { name: "保存修改" }).click();

  await expect(page.getByText("REPORT REVIEW / v2")).toBeVisible();
  await expect(page.getByLabel("评分摘要")).toContainText("88 · B");
  await expect(page.getByRole("link", { name: "查看版本 1" })).toBeVisible();
  await page.getByLabel("审核评语（可选）").fill("基于教师修订重新运行 Agent。");
  await page.getByRole("button", { name: "重新评估" }).click();

  await expect(page.getByRole("link", { name: "查看版本 3" })).toBeVisible();
  await page.getByRole("link", { name: "查看版本 3" }).click();
  await expect(page.getByText("REPORT REVIEW / v3")).toBeVisible();
  await expect(page.getByLabel("评分摘要")).toContainText("72 · C");
  await expect(page.getByLabel("评估边界")).toContainText("proposed");
  await expect(page.getByRole("link", { name: "查看版本 1" })).toBeVisible();
  await expect(page.getByRole("link", { name: "查看版本 2" })).toBeVisible();

  await teacher.close();
});
