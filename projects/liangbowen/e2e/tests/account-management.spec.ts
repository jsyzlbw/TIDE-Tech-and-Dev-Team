import { expect, type APIRequestContext, type Page, test } from "@playwright/test";

const apiBaseUrl = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1";
const SEEDED_ASSIGNMENT_ID = "00000000-0000-4000-8000-000000000101";

async function login(page: Page, username: string, password: string) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill(username);
  await page.getByRole("textbox", { name: "密码", exact: true }).fill(password);
  await page.getByRole("button", { name: "登录并进入工作台" }).click();
}

async function tokenFor(
  request: APIRequestContext,
  username: string,
  password: string,
) {
  const response = await request.post(`${apiBaseUrl}/auth/login`, {
    data: { username, password },
  });
  expect(
    response.ok(),
    `login for ${username} returned HTTP ${response.status()}`,
  ).toBe(true);
  return (await response.json() as { access_token: string }).access_token;
}

async function createAccount(
  page: Page,
  account: {
    username: string;
    displayName: string;
    password: string;
    role?: "teacher" | "student";
  },
) {
  await page.getByRole("button", { name: /创建(?:学生)?账号/u }).click();
  await page.getByLabel("用户名").fill(account.username);
  await page.getByLabel("显示姓名").fill(account.displayName);
  if (account.role !== undefined) {
    await page.getByRole("combobox", { name: "账号角色" }).selectOption(account.role);
  }
  await page.getByLabel("初始密码", { exact: true }).fill(account.password);
  await page.getByLabel("确认初始密码").fill(account.password);
  await page.getByRole("button", { name: /创建(?:学生)?账号/u }).last().click();
  await expect(page.getByText(`${account.username} 已创建。`)).toBeVisible();
}

test("administrator creates teacher and teacher creates student", async ({ browser, request }) => {
  const suffix = `${Date.now().toString(36)}-${process.pid.toString(36)}`;
  const teacherUsername = `teacher-${suffix}`;
  const studentUsername = `student-${suffix}`;

  const adminContext = await browser.newContext();
  const teacherContext = await browser.newContext();
  const studentContext = await browser.newContext();
  try {
    const adminPage = await adminContext.newPage();
    await login(adminPage, "admin", "Admin123!Secure");
    await expect(adminPage).toHaveURL(/\/admin\/users$/u);
    await createAccount(adminPage, {
      username: teacherUsername,
      displayName: "验收教师",
      role: "teacher",
      password: "Course2026!Secure",
    });

    const teacherPage = await teacherContext.newPage();
    await login(teacherPage, teacherUsername, "Course2026!Secure");
    await expect(teacherPage).toHaveURL(/\/teacher$/u);
    await teacherPage.getByRole("link", { name: "管理学生账号" }).click();
    await expect(teacherPage).toHaveURL(/\/teacher\/users$/u);
    await createAccount(teacherPage, {
      username: studentUsername,
      displayName: "验收学生",
      password: "Student2026!Secure",
    });

    const studentPage = await studentContext.newPage();
    await login(studentPage, studentUsername, "Student2026!Secure");
    await expect(studentPage).toHaveURL(/\/student$/u);
    await expect(studentPage.getByText("图的最短路径")).toBeVisible();

    const studentToken = await tokenFor(request, studentUsername, "Student2026!Secure");
    const forbidden = await request.get(`${apiBaseUrl}/users`, {
      headers: { Authorization: `Bearer ${studentToken}` },
    });
    expect(forbidden.status()).toBe(403);

    for (const forbiddenPath of ["/teacher/users", "/admin/users"]) {
      await studentPage.goto(forbiddenPath);
      await expect(
        studentPage.getByRole("heading", { name: "无权访问此页面" }),
      ).toBeVisible();
      await expect(
        studentPage.getByRole("heading", { name: /账号管理台/u }),
      ).toHaveCount(0);
      await studentPage.getByRole("link", { name: "去自己的工作台" }).click();
      await expect(studentPage).toHaveURL(/\/student$/u);
      await expect(studentPage.getByText("图的最短路径")).toBeVisible();
    }
  } finally {
    await Promise.all([
      adminContext.close(),
      teacherContext.close(),
      studentContext.close(),
    ]);
  }
});

test("administrator cannot read assignments or open teacher pages", async ({ browser, request }) => {
  const adminToken = await tokenFor(request, "admin", "Admin123!Secure");
  const headers = { Authorization: `Bearer ${adminToken}` };

  const [assignmentList, assignmentDetail] = await Promise.all([
    request.get(`${apiBaseUrl}/assignments`, { headers }),
    request.get(`${apiBaseUrl}/assignments/${SEEDED_ASSIGNMENT_ID}`, { headers }),
  ]);
  expect(assignmentList.status()).toBe(403);
  expect(assignmentDetail.status()).toBe(403);

  const adminContext = await browser.newContext();
  try {
    const adminPage = await adminContext.newPage();
    await login(adminPage, "admin", "Admin123!Secure");
    await expect(adminPage).toHaveURL(/\/admin\/users$/u);

    await adminPage.goto("/teacher");
    await expect(adminPage.getByRole("heading", { name: "无权访问此页面" })).toBeVisible();
    await expect(adminPage.getByRole("heading", { name: "教师评阅工作台" })).toHaveCount(0);

    await adminPage.goto(`/teacher/assignments/${SEEDED_ASSIGNMENT_ID}`);
    await expect(adminPage.getByRole("heading", { name: "无权访问此页面" })).toBeVisible();
    await expect(adminPage.getByText("图的最短路径")).toHaveCount(0);
  } finally {
    await adminContext.close();
  }
});
