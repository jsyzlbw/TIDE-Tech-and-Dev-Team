import { expect, type APIRequestContext, test } from "@playwright/test";

const apiBaseUrl = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1";
const commandToken = process.env.E2E_MATTERMOST_COMMAND_TOKEN ?? "e2e-command-token";
const setupKey = process.env.E2E_MATTERMOST_DEMO_SETUP_KEY ?? "e2e-demo-setup-key";
const teacherMattermostId = "teacher0000000000000000000";
const studentMattermostId = "student0000000000000000000";

async function login(request: APIRequestContext, username: string, password: string) {
  const response = await request.post(`${apiBaseUrl}/auth/login`, {
    data: { username, password },
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json() as { access_token: string }).access_token;
}

async function bind(
  request: APIRequestContext,
  localUsername: string,
  mattermostUserId: string,
) {
  const response = await request.post(`${apiBaseUrl}/integrations/mattermost/demo-bindings`, {
    headers: { "x-demo-setup-key": setupKey },
    data: {
      local_username: localUsername,
      mattermost_user_id: mattermostUserId,
      mattermost_username: `${localUsername}-mm`,
    },
  });
  expect([200, 201]).toContain(response.status());
}

async function command(
  request: APIRequestContext,
  actor: { id: string; username: string },
  text: string,
  trigger: string,
) {
  const response = await request.post(`${apiBaseUrl}/integrations/mattermost/commands`, {
    form: {
      token: commandToken,
      team_id: "team0000000000000000000000",
      team_domain: "course",
      channel_id: "channel0000000000000000000",
      channel_name: "homework",
      user_id: actor.id,
      user_name: actor.username,
      command: "/hw",
      text,
      trigger_id: trigger,
      response_url: "http://127.0.0.1:8065/hooks/response",
    },
  });
  expect(response.status()).toBe(200);
  const payload = await response.json() as { response_type: string; text: string };
  expect(["ephemeral", "in_channel"]).toContain(payload.response_type);
  expect(payload.text.length).toBeGreaterThan(0);
  return payload;
}

test("URL-encoded /hw list, submit, and summary agree with REST state", async ({ request }) => {
  await bind(request, "teacher", teacherMattermostId);
  await bind(request, "student1", studentMattermostId);

  const list = await command(
    request,
    { id: studentMattermostId, username: "student1-mm" },
    "list",
    "e2e-trigger-list",
  );
  expect(list.response_type).toBe("ephemeral");
  expect(list.text).toContain("HW-0001｜图的最短路径｜published");

  const submit = await command(
    request,
    { id: studentMattermostId, username: "student1-mm" },
    'submit HW-0001 --text "使用优先队列完成松弛"',
    "e2e-trigger-submit",
  );
  expect(submit.text).toBe("已提交 HW-0001，版本 2。");

  const studentToken = await login(request, "student1", "Student123!");
  const assignmentsResponse = await request.get(`${apiBaseUrl}/assignments?limit=20&offset=0`, {
    headers: { authorization: `Bearer ${studentToken}` },
  });
  expect(assignmentsResponse.ok()).toBeTruthy();
  const assignments = await assignmentsResponse.json() as Array<{ id: string; code: string }>;
  const assignment = assignments.find((item) => item.code === "HW-0001");
  expect(assignment).toBeDefined();
  const submissionsResponse = await request.get(
    `${apiBaseUrl}/assignments/${assignment?.id}/submissions/me?limit=20&offset=0`,
    { headers: { authorization: `Bearer ${studentToken}` } },
  );
  expect(submissionsResponse.ok()).toBeTruthy();
  const submissions = await submissionsResponse.json() as Array<{
    version: number;
    source: string;
    content_text: string;
  }>;
  expect(submissions[0]).toMatchObject({
    version: 2,
    source: "mattermost",
    content_text: "使用优先队列完成松弛",
  });

  const teacherToken = await login(request, "teacher", "Teacher123!");
  const summaryResponse = await request.get(
    `${apiBaseUrl}/assignments/${assignment?.id}/summary?limit=100&offset=0`,
    { headers: { authorization: `Bearer ${teacherToken}` } },
  );
  expect(summaryResponse.ok()).toBeTruthy();
  const restSummary = await summaryResponse.json() as {
    total_students: number;
    submitted_students: number;
    students: Array<{ username: string; latest_version: number; latest_submission: { source: string } }>;
  };

  const summary = await command(
    request,
    { id: teacherMattermostId, username: "teacher-mm" },
    "summary HW-0001",
    "e2e-trigger-summary",
  );
  expect(summary.text).toContain(
    `HW-0001 summary: total=${restSummary.total_students}, `
      + `submitted=${restSummary.submitted_students}, `
      + `missing=${restSummary.total_students - restSummary.submitted_students}`,
  );

  expect(restSummary.submitted_students).toBe(3);
  expect(restSummary.students.find((item) => item.username === "student1")).toMatchObject({
    latest_version: 2,
    latest_submission: { source: "mattermost" },
  });
});
