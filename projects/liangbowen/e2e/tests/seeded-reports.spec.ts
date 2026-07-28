import { expect, type APIRequestContext, type Page, test } from "@playwright/test";

const apiBaseUrl = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1";

type Report = {
  id: string;
  origin: string;
  score: number;
  grade: string;
  review_status: string;
  validation_status: string;
  major_issues: Array<{ title?: string }>;
};

const seededReports = [
  {
    submissionId: "b81ae676-41d2-5d6a-8107-e85477fc9761",
    score: 94,
    grade: "A",
    completeness: "完整",
    correctness: "正确",
    diagnosis: "术语可以更精确",
  },
  {
    submissionId: "4a49e6e8-9b2a-50c1-b0ea-3d5b10a64270",
    score: 72,
    grade: "C",
    completeness: "部分完整",
    correctness: "基本正确",
    diagnosis: "缺少非负权限制",
  },
  {
    submissionId: "d474b406-be8d-5c89-9e53-63f0ecdf1e71",
    score: 35,
    grade: "D",
    completeness: "不完整",
    correctness: "错误",
    diagnosis: "选择规则错误",
  },
] as const;

async function loginAsTeacher(page: Page) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill("teacher");
  await page.getByRole("textbox", { name: "密码", exact: true }).fill("Teacher123!");
  await page.getByRole("button", { name: "登录并进入工作台" }).click();
  await expect(page.getByRole("heading", { name: "教师评阅工作台" })).toBeVisible();
}

async function teacherToken(request: APIRequestContext) {
  const response = await request.post(`${apiBaseUrl}/auth/login`, {
    data: { username: "teacher", password: "Teacher123!" },
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json() as { access_token: string }).access_token;
}

test("teacher can reach all three seeded report variants after later submissions", async ({ page, request }) => {
  await loginAsTeacher(page);
  const token = await teacherToken(request);

  for (const expected of seededReports) {
    const response = await request.get(
      `${apiBaseUrl}/submissions/${expected.submissionId}/reports?limit=100&offset=0`,
      { headers: { authorization: `Bearer ${token}` } },
    );
    expect(response.ok()).toBeTruthy();
    const reports = await response.json() as Report[];
    const report = reports.find((candidate) =>
      candidate.origin === "agent"
      && candidate.score === expected.score
      && candidate.grade === expected.grade
      && candidate.major_issues.some((issue) => issue.title === expected.diagnosis),
    );
    expect(report, `missing seeded ${expected.score}/${expected.grade} historical report`).toBeDefined();

    await page.goto(`/teacher/reports/${report?.id}`);
    await expect(page.getByRole("heading", { name: "评估报告审阅" })).toBeVisible();
    await expect(page.getByLabel("评分摘要")).toContainText(`${expected.score} · ${expected.grade}`);
    await expect(page.getByRole("heading", { name: "答案完整性" }).locator("..")).toContainText(expected.completeness);
    await expect(page.getByRole("heading", { name: "正确性初步判断" }).locator("..")).toContainText(expected.correctness);
    await expect(page.getByText(expected.diagnosis, { exact: true })).toBeVisible();
    await expect(page.getByLabel("评估边界")).toContainText("已通过");
    await expect(page.getByLabel("评估边界")).toContainText(/proposed|confirmed|modified|superseded/u);

    const visiblePage = await page.locator("body").innerText();
    for (const secretMarker of [
      "e2e-command-token-26chars",
      "e2e-demo-setup-key-26chars",
      "Teacher123!",
      "Student123!",
      "access_token",
      "Bearer ",
    ]) {
      expect(visiblePage).not.toContain(secretMarker);
    }
  }
});
