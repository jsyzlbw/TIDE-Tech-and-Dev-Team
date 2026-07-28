# Verification and Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the backend, Agent/Mattermost workflow, and web console into one reproducible acceptance package with a one-command runtime, six required test scenarios, three demonstrable reports, complete documentation, and a timed 15–20 minute presentation path.

**Architecture:** Docker Compose is the system boundary: PostgreSQL and Redis support the API and Celery worker, Nginx serves the React console, and an opt-in profile adds Mattermost plus its database. Deterministic demo seeding and a machine-readable verification script make the acceptance evidence repeatable instead of dependent on live AI output.

**Tech Stack:** Docker Compose, Mattermost Team Edition, PostgreSQL 16, Redis 7, Nginx, FastAPI, Celery, React, pytest, Playwright, Python 3.12, Markdown

---

## Frozen acceptance contract

- Default demo URL: `http://localhost:8080`
- API URL: `http://localhost:8000/api/v1`
- Mattermost URL when the profile is enabled: `http://localhost:8065`
- Demo identity bootstrap key: local-only `DEMO_SETUP_KEY`, generated in `.env` and never committed
- Demo teacher: `teacher` / `Teacher123!`
- Demo students: `student1`, `student2`, `student3` / `Student123!`
- Seed assignment code: `HW-0001`
- Required report cases: complete/correct at 94/A, partially correct at 72/C, clearly incorrect at 35/D
- Required test cases: complete, partial, incorrect, incomplete/ambiguous, malformed Agent output, current capability boundary
- Required final commands: `make demo`, `make verify`, `make demo-full`
- Acceptance evidence path: `artifacts/acceptance/`

## Task 1: Package the frontend and complete the default Compose runtime

**Files:**
- Create: `frontend/Dockerfile`
- Create: `frontend/nginx.conf`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `.gitignore`
- Create: `backend/tests/ops/test_compose_contract.py`

- [ ] **Step 1: Write the Compose contract test**

Create `backend/tests/ops/test_compose_contract.py`:

```python
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_default_runtime_has_health_checked_dependencies():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]

    assert {"api", "worker", "web", "postgres", "redis"} <= services.keys()
    assert services["api"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["web"]["ports"] == ["8080:80"]


def test_mattermost_is_opt_in():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert compose["services"]["mattermost"]["profiles"] == ["mattermost"]
    assert compose["services"]["mattermost-db"]["profiles"] == ["mattermost"]
```

Add `pyyaml>=6.0` to the backend `dev` dependency list.

- [ ] **Step 2: Run the contract test and verify failure**

```bash
.venv/bin/pytest backend/tests/ops/test_compose_contract.py -q
```

Expected: failure because the web and Mattermost services are not present yet.

- [ ] **Step 3: Create the production frontend image**

Create `frontend/Dockerfile`:

```dockerfile
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --retries=10 CMD wget -qO- http://127.0.0.1/healthz || exit 1
```

Create `frontend/nginx.conf`:

```nginx
server {
    listen 80;
    server_name _;

    location = /healthz {
        access_log off;
        add_header Content-Type text/plain;
        return 200 "ok\n";
    }

    location /api/ {
        proxy_pass http://api:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location / {
        root /usr/share/nginx/html;
        try_files $uri $uri/ /index.html;
    }
}
```

- [ ] **Step 4: Add web and Mattermost services**

Extend `docker-compose.yml` with these service definitions and preserve the API, worker, PostgreSQL, and Redis definitions from the foundation plan:

```yaml
  web:
    build:
      context: ./frontend
    ports:
      - "8080:80"
    depends_on:
      api:
        condition: service_healthy

  mattermost-db:
    image: postgres:16-alpine
    profiles: ["mattermost"]
    environment:
      POSTGRES_DB: mattermost
      POSTGRES_USER: mattermost
      POSTGRES_PASSWORD: ${MATTERMOST_DB_PASSWORD:-mattermost}
    volumes:
      - mattermost-db-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U mattermost -d mattermost"]
      interval: 5s
      timeout: 3s
      retries: 20

  mattermost:
    image: mattermost/mattermost-team-edition:10.5
    profiles: ["mattermost"]
    ports:
      - "8065:8065"
    environment:
      MM_SQLSETTINGS_DRIVERNAME: postgres
      MM_SQLSETTINGS_DATASOURCE: postgres://mattermost:${MATTERMOST_DB_PASSWORD:-mattermost}@mattermost-db:5432/mattermost?sslmode=disable&connect_timeout=10
      MM_SERVICESETTINGS_SITEURL: http://localhost:8065
      MM_SERVICESETTINGS_ENABLELOCALMODE: "true"
      MM_SERVICESETTINGS_ENABLEDEVELOPER: "true"
      MM_SERVICESETTINGS_ENABLEUSERACCESSTOKENS: "true"
    depends_on:
      mattermost-db:
        condition: service_healthy
    volumes:
      - mattermost-data:/mattermost/data
      - mattermost-config:/mattermost/config

volumes:
  postgres-data:
  mattermost-db-data:
  mattermost-data:
  mattermost-config:
```

Add every Mattermost/API key used by Compose to `.env.example`, with safe local defaults and comments that real provider keys must never be committed. Add `.env`, `artifacts/acceptance/*`, and `!artifacts/acceptance/.gitkeep` to `.gitignore`.

- [ ] **Step 5: Verify the default runtime**

```bash
.venv/bin/pytest backend/tests/ops/test_compose_contract.py -q
docker compose config --quiet
docker compose build api worker web
docker compose up -d postgres redis api worker web
curl --fail http://localhost:8000/health/ready
curl --fail http://localhost:8080/healthz
docker compose down
```

Expected: the test passes, Compose validates, both health calls return HTTP 200, and shutdown exits `0`.

- [ ] **Step 6: Commit the packaged runtime**

```bash
git add frontend/Dockerfile frontend/nginx.conf docker-compose.yml .env.example .gitignore backend
git commit -m "build: package the complete local runtime"
```

## Task 2: Make Mattermost setup deterministic and idempotent

**Files:**
- Create: `scripts/configure_mattermost.py`
- Create: `scripts/tests/test_configure_mattermost.py`
- Modify: `backend/pyproject.toml`
- Modify: `Makefile`

- [ ] **Step 1: Write configuration-planning tests**

Create `scripts/tests/test_configure_mattermost.py`:

```python
from configure_mattermost import desired_commands, desired_users


def test_demo_users_cover_both_roles():
    users = desired_users()
    assert {user["username"] for user in users} == {
        "teacher",
        "student1",
        "student2",
        "student3",
    }


def test_slash_commands_target_the_api_adapter():
    commands = desired_commands("http://api:8000")
    assert [command["trigger"] for command in commands] == ["hw"]
    assert all(
        command["url"].endswith("/api/v1/integrations/mattermost/commands")
        for command in commands
    )
```

- [ ] **Step 2: Run and verify failure**

```bash
PYTHONPATH=scripts .venv/bin/pytest scripts/tests/test_configure_mattermost.py -q
```

Expected: import failure because `configure_mattermost.py` does not exist.

- [ ] **Step 3: Implement desired state and API reconciliation**

Create `scripts/configure_mattermost.py` with these public functions:

```python
import argparse
import os
import time

import httpx


def desired_users() -> list[dict[str, str]]:
    return [
        {"email": "teacher@example.com", "username": "teacher", "password": "Teacher123!"},
        {"email": "student1@example.com", "username": "student1", "password": "Student123!"},
        {"email": "student2@example.com", "username": "student2", "password": "Student123!"},
        {"email": "student3@example.com", "username": "student3", "password": "Student123!"},
    ]


def desired_commands(api_origin: str) -> list[dict[str, object]]:
    endpoint = f"{api_origin.rstrip('/')}/api/v1/integrations/mattermost/commands"
    return [
        {
            "trigger": "hw",
            "display_name": "AI 作业评审",
            "description": "发布、查询、提交、汇总和评估作业",
            "auto_complete": True,
            "auto_complete_hint": "help | publish | list | show | submit | summary | evaluate",
            "url": endpoint,
        },
    ]
```

The executable `main()` must:

1. poll `/api/v4/system/ping` for at most 120 seconds;
2. create the first admin account if login returns 401;
3. log in and retain the `Token` response header;
4. get-or-create the team `data-structures` and channel `course-home`;
5. get-or-create the four users and add them to the team/channel;
6. get-or-create `/hw`, matching by trigger word and updating its URL/autocomplete fields when configuration changed;
7. POST the four `{local_username, mattermost_user_id, mattermost_username}` mappings to the API development-only demo-binding endpoint with `X-Demo-Setup-Key`;
8. print the team, channel, identity, and command IDs as JSON without printing passwords, setup keys, or tokens.

Use `httpx.Client(timeout=10)` and treat HTTP 200/201 as success and HTTP 400 with an explicit “already exists” code as recoverable. All other responses must call `raise_for_status()`.

- [ ] **Step 4: Add repeatable commands**

Add to `Makefile`:

```makefile
mattermost-up:
	docker compose --profile mattermost up -d

mattermost-configure:
	.venv/bin/python scripts/configure_mattermost.py \
		--base-url http://localhost:8065 \
		--api-origin http://api:8000 \
		--setup-origin http://localhost:8000

demo-full: mattermost-up mattermost-configure demo-seed
```

- [ ] **Step 5: Verify test and idempotence**

```bash
PYTHONPATH=scripts .venv/bin/pytest scripts/tests/test_configure_mattermost.py -q
make mattermost-up
make mattermost-configure
make mattermost-configure
curl --fail http://localhost:8065/api/v4/system/ping
```

Expected: both configuration runs exit `0`; the second run reports existing resources instead of duplicating them.

- [ ] **Step 6: Commit Mattermost automation**

```bash
git add scripts/configure_mattermost.py scripts/tests backend/pyproject.toml Makefile
git commit -m "feat: automate Mattermost demo setup"
```

## Task 3: Seed three stable report cases and verify their relationships

**Files:**
- Create: `backend/app/demo/__init__.py`
- Create: `backend/app/demo/scenarios.py`
- Modify: `backend/scripts/seed_demo.py`
- Create: `backend/tests/demo/test_scenarios.py`

- [ ] **Step 1: Write deterministic scenario tests**

Create `backend/tests/demo/test_scenarios.py`:

```python
from app.demo.scenarios import DEMO_SCENARIOS


def test_demo_scenarios_cover_required_report_examples():
    assert [case.key for case in DEMO_SCENARIOS] == ["complete", "partial", "incorrect"]
    assert [case.expected_grade for case in DEMO_SCENARIOS] == ["A", "C", "D"]
    assert all(case.answer.strip() for case in DEMO_SCENARIOS)


def test_demo_scenarios_use_distinct_students():
    assert len({case.student_username for case in DEMO_SCENARIOS}) == 3
```

- [ ] **Step 2: Run and verify failure**

```bash
.venv/bin/pytest backend/tests/demo/test_scenarios.py -q
```

Expected: collection fails because demo scenarios are missing.

- [ ] **Step 3: Define the three report cases**

Create `backend/app/demo/scenarios.py` with a frozen `DemoScenario` dataclass and three records:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class DemoScenario:
    key: str
    student_username: str
    answer: str
    expected_grade: str


DEMO_SCENARIOS = (
    DemoScenario(
        key="complete",
        student_username="student1",
        answer="使用邻接表和优先队列实现 Dijkstra；初始化 dist[s]=0，每次取最短未确定顶点并松弛边。复杂度 O((V+E)logV)，仅适用于非负权边。",
        expected_grade="A",
    ),
    DemoScenario(
        key="partial",
        student_username="student2",
        answer="维护到各点的最短距离，每次选择距离最小的点，再更新相邻点。",
        expected_grade="C",
    ),
    DemoScenario(
        key="incorrect",
        student_username="student3",
        answer="对所有边按权重排序，依次选最小边，得到源点到其他点的最短路。",
        expected_grade="D",
    ),
)
```

- [ ] **Step 4: Extend the seed command**

Update `backend/scripts/seed_demo.py` so one transaction:

- upserts the fixed teacher and three fixed students by local username;
- upserts `HW-0001` with a Dijkstra prompt, a four-point rubric, and a future deadline;
- publishes the assignment;
- creates exactly one active submission version for each scenario;
- invokes the Mock provider through `EvaluationService`, never by inserting reports directly;
- asserts the resulting scores/grades are 94/A, 72/C, and 35/D;
- prints JSON containing `assignment_id`, the three `submission_id` values, and the three `report_id` values.

The seed must accept `--reset-demo` to delete only records tagged `metadata.demo_key`, and must refuse a reset when `APP_ENV=production`.

- [ ] **Step 5: Verify repeatability**

```bash
.venv/bin/pytest backend/tests/demo/test_scenarios.py -q
docker compose up -d postgres redis api worker
docker compose exec api python -m scripts.seed_demo --reset-demo
docker compose exec api python -m scripts.seed_demo
```

Expected: both seed calls exit `0`; the second call returns the same logical assignment and does not add duplicate active submissions.

- [ ] **Step 6: Commit demo data**

```bash
git add backend/app/demo backend/scripts/seed_demo.py backend/tests/demo
git commit -m "feat: add deterministic acceptance scenarios"
```

## Task 4: Encode all six required capability tests

**Files:**
- Create: `backend/tests/acceptance/cases.py`
- Create: `backend/tests/acceptance/test_required_scenarios.py`
- Create: `backend/tests/acceptance/test_capability_boundaries.py`
- Modify: `backend/app/evaluations/mock_provider.py`

- [ ] **Step 1: Define the test matrix as data**

Create `backend/tests/acceptance/cases.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class RequiredCase:
    key: str
    answer: str
    expected_grade: str | None
    expected_review: bool


REQUIRED_CASES = (
    RequiredCase("complete", "Dijkstra 完整答案，包含松弛、复杂度与非负权限制。", "A", False),
    RequiredCase("partial", "每次选择距离最小顶点并更新邻接点。", "C", True),
    RequiredCase("incorrect", "排序所有边即可得到单源最短路。", "D", True),
    RequiredCase("ambiguous", "大概遍历一下，具体步骤不确定。", None, True),
)
```

- [ ] **Step 2: Write the four answer-quality tests**

Create `backend/tests/acceptance/test_required_scenarios.py`:

```python
import pytest

from app.db.types import SubmissionContentType, SubmissionSource
from app.evaluations.types import JobReason
from app.submissions.schemas import SubmissionCreate
from app.submissions.service import create_submission
from tests.acceptance.cases import REQUIRED_CASES


@pytest.mark.asyncio
@pytest.mark.parametrize("case", REQUIRED_CASES, ids=lambda item: item.key)
async def test_required_answer_scenario(
    db_session, evaluation_service, published_assignment, student, case
):
    submission = await create_submission(
        db_session,
        published_assignment,
        student.id,
        SubmissionCreate(
            content_type=SubmissionContentType.TEXT,
            content_text=case.answer,
            content_json={"scenario_key": case.key},
        ),
        SubmissionSource.WEB,
    )
    report = await evaluation_service.evaluate_now(submission.id, reason=JobReason.INITIAL)

    if case.expected_grade is not None:
        assert report.grade.value == case.expected_grade
    assert report.requires_human_review is case.expected_review
    assert report.answer_completeness
    assert report.correctness
    assert report.suggestions
```

Create `backend/tests/acceptance/conftest.py` with an `evaluation_service` fixture that passes `db_session`, `EvaluationEngine(MockEvaluationProvider(FIXTURE_DIR))`, and the eager/no-dispatch test setting into `EvaluationService`. Add fixtures for a three-result repair provider (non-JSON, score 101, valid JSON), an always-invalid provider, an offline provider, a code submission, a no-rubric assignment, an ambiguous assignment, and a prompt-injection submission. Build records through assignment/submission services. Each fixture must use the same `db_session`; do not create a second engine or insert reports directly.

- [ ] **Step 3: Write malformed-output and boundary tests**

Create `backend/tests/acceptance/test_capability_boundaries.py`:

```python
import pytest
from sqlalchemy import select

from app.db.types import SubmissionSource
from app.evaluations.engine import EvaluationExhausted
from app.evaluations.providers.base import ProviderUnavailable
from app.evaluations.types import JobReason, ValidationStatus
from app.submissions.model import Submission
from app.submissions.schemas import SubmissionCreate
from app.submissions.service import SubmissionTooLong, create_submission


@pytest.mark.asyncio
async def test_invalid_output_is_repaired_on_third_attempt(
    evaluation_service_with_repair_provider,
    submitted_answer,
):
    report = await evaluation_service_with_repair_provider.evaluate_now(
        submitted_answer.id,
        reason=JobReason.INITIAL,
    )
    job = await evaluation_service_with_repair_provider.get_latest_job(submitted_answer.id)
    assert report.validation_status is ValidationStatus.REPAIRED
    assert job.attempt_count == 3
    assert len(job.validation_failures) == 2


@pytest.mark.asyncio
async def test_malformed_agent_output_is_recorded_after_repair_budget(
    evaluation_service_with_invalid_provider,
    submitted_answer,
):
    with pytest.raises(EvaluationExhausted):
        await evaluation_service_with_invalid_provider.evaluate_now(
            submitted_answer.id,
            reason=JobReason.INITIAL,
        )

    job = await evaluation_service_with_invalid_provider.get_latest_job(submitted_answer.id)
    assert job.status.value == "failed"
    assert job.attempt_count == 3
    assert "raw output omitted" in job.safe_error_message


@pytest.mark.asyncio
async def test_code_execution_boundary_is_disclosed(evaluation_service, code_submission):
    report = await evaluation_service.evaluate_now(code_submission.id, reason=JobReason.INITIAL)
    assert report.requires_human_review is True
    assert any("未运行代码" in item for item in report.limitations)
    assert report.audit_metadata["code_executed"] is False


@pytest.mark.asyncio
async def test_missing_rubric_reduces_confidence(
    evaluation_service,
    no_rubric_submission,
):
    report = await evaluation_service.evaluate_now(
        no_rubric_submission.id,
        reason=JobReason.INITIAL,
    )
    assert report.confidence <= 0.5
    assert report.requires_human_review is True
    assert any("缺少评分要点" in item for item in report.limitations)


@pytest.mark.asyncio
async def test_oversized_answer_is_rejected_without_losing_prior_version(
    db_session,
    published_assignment,
    student,
    submitted_answer,
):
    with pytest.raises(SubmissionTooLong):
        await create_submission(
            db_session,
            published_assignment,
            student.id,
            SubmissionCreate(content_type="text", content_text="x" * 50_001),
            SubmissionSource.WEB,
        )
    latest_id = await db_session.scalar(
        select(Submission.id)
        .where(
            Submission.assignment_id == published_assignment.id,
            Submission.student_id == student.id,
        )
        .order_by(Submission.version.desc())
        .limit(1)
    )
    assert latest_id == submitted_answer.id


@pytest.mark.asyncio
async def test_provider_outage_fails_job_but_preserves_submission(
    evaluation_service_with_offline_provider,
    submitted_answer,
    db_session,
):
    with pytest.raises(ProviderUnavailable):
        await evaluation_service_with_offline_provider.evaluate_now(
            submitted_answer.id,
            reason=JobReason.INITIAL,
        )
    assert await db_session.get(Submission, submitted_answer.id) is not None
    job = await evaluation_service_with_offline_provider.get_latest_job(submitted_answer.id)
    assert job.status.value == "failed"


@pytest.mark.asyncio
async def test_ambiguous_prompt_requires_teacher_review(
    evaluation_service,
    ambiguous_prompt_submission,
):
    report = await evaluation_service.evaluate_now(
        ambiguous_prompt_submission.id,
        reason=JobReason.INITIAL,
    )
    assert report.requires_human_review is True
    assert report.confidence <= 0.5


@pytest.mark.asyncio
async def test_prompt_injection_cannot_change_schema_or_grade_rule(
    evaluation_service_with_injection_provider,
    injection_submission,
):
    report = await evaluation_service_with_injection_provider.evaluate_now(
        injection_submission.id,
        reason=JobReason.INITIAL,
    )
    assert report.schema_version == "1.0"
    assert report.score == 94
    assert report.grade.value == "A"
    assert report.requires_human_review is True
```

The injection provider deliberately returns score `94` with grade `D`; the assertion proves backend normalization, while the captured provider request test from Phase 2 proves the student text remains inside the `<student_answer>` data boundary.

- [ ] **Step 4: Make Mock provider behavior explicit**

Update `MockEvaluationProvider` to select a fixture only from the explicit `scenario_key` carried in demo/test request metadata. Production requests must never infer a grade from keyword matching. If no scenario key is present, return a neutral partial report that requires human review.

- [ ] **Step 5: Run the acceptance suite**

```bash
.venv/bin/pytest backend/tests/acceptance -q
```

Expected: the four answer-quality cases, repaired-output path, exhausted-output path, and all six capability-boundary assertions pass. Pytest output includes explicit names for complete, partial, incorrect, ambiguous, code-not-executed, missing rubric, overlength, provider outage, ambiguous prompt, and prompt injection.

- [ ] **Step 6: Commit the acceptance matrix**

```bash
git add backend/tests/acceptance backend/app/evaluations/mock_provider.py
git commit -m "test: cover the required acceptance scenarios"
```

## Task 5: Automate the complete browser journey

**Files:**
- Create: `e2e/package.json`
- Create: `e2e/playwright.config.ts`
- Create: `e2e/tests/full-flow.spec.ts`
- Create: `e2e/tests/mattermost-adapter.spec.ts`
- Create: `e2e/tsconfig.json`
- Modify: `Makefile`

- [ ] **Step 1: Create the isolated E2E package**

Create `e2e/package.json`:

```json
{
  "name": "ai-grading-e2e",
  "private": true,
  "type": "module",
  "scripts": {
    "test": "playwright test",
    "report": "playwright show-report"
  },
  "devDependencies": {
    "@playwright/test": "^1.50.0",
    "typescript": "^5.7.0"
  }
}
```

Create `e2e/playwright.config.ts`:

```ts
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 45_000,
  retries: 1,
  reporter: [["list"], ["html", { outputFolder: "../artifacts/acceptance/playwright" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8080",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
```

- [ ] **Step 2: Write the failing complete-flow test**

Create `e2e/tests/full-flow.spec.ts`:

```ts
import { expect, test } from "@playwright/test";

test("teacher publishes, student submits, agent evaluates, teacher reviews", async ({ browser }) => {
  const teacher = await browser.newContext();
  const teacherPage = await teacher.newPage();
  await teacherPage.goto("/login");
  await teacherPage.getByLabel("用户名").fill("teacher");
  await teacherPage.getByLabel("密码").fill("Teacher123!");
  await teacherPage.getByRole("button", { name: "登录" }).click();
  await teacherPage.getByRole("button", { name: "新建作业" }).click();
  await teacherPage.getByLabel("作业标题").fill("验收：Dijkstra 最短路");
  await teacherPage.getByLabel("题目内容").fill("说明算法、复杂度与适用条件。");
  await teacherPage.getByRole("button", { name: "发布作业" }).click();
  const code = await teacherPage.getByTestId("assignment-code").textContent();
  expect(code).toMatch(/^HW-\d{4}$/);

  const student = await browser.newContext();
  const studentPage = await student.newPage();
  await studentPage.goto("/login");
  await studentPage.getByLabel("用户名").fill("student1");
  await studentPage.getByLabel("密码").fill("Student123!");
  await studentPage.getByRole("button", { name: "登录" }).click();
  await studentPage.getByText(code!).click();
  await studentPage.getByLabel("作业答案").fill(
    "使用邻接表和优先队列进行松弛，复杂度 O((V+E)logV)，要求非负权边。",
  );
  await studentPage.getByRole("button", { name: "提交答案" }).click();
  await expect(studentPage.getByText("已提交 · 版本 1")).toBeVisible();

  await teacherPage.reload();
  await teacherPage.getByText(code!).click();
  await teacherPage.getByRole("button", { name: "生成待评报告" }).click();
  await expect(teacherPage.getByText("待教师审核")).toBeVisible({ timeout: 30_000 });
  await teacherPage.getByRole("button", { name: "确认报告" }).click();
  await expect(teacherPage.getByText("教师已确认")).toBeVisible();

  await teacher.close();
  await student.close();
});
```

- [ ] **Step 3: Add re-evaluation and adapter contract coverage**

In the same test file, add a second test that opens an existing report, edits score and comment, saves a modified teacher version, requests re-evaluation, and asserts that the prior version remains visible in history while the new Agent report becomes the proposed version.

Create `e2e/tests/mattermost-adapter.spec.ts` to POST URL-encoded Mattermost payloads with `command=/hw` and `text=list`, `text=submit HW-0001 --text "使用优先队列完成松弛"`, and `text=summary HW-0001` to the integration endpoint with the configured Mattermost token. Assert Mattermost-compatible response shapes and verify the resulting records through the REST API. This isolates adapter correctness from browser rendering while the live demo proves the Mattermost UI.

- [ ] **Step 4: Add Make targets and execute**

Add to `Makefile`:

```makefile
e2e-install:
	cd e2e && npm ci && npx playwright install chromium

e2e:
	cd e2e && npm test
```

Run:

```bash
cd e2e && npm install && npx playwright install chromium
cd ..
make demo
make e2e
```

Expected: both browser-flow tests and the adapter contract test pass; an HTML report is written under `artifacts/acceptance/playwright/`.

- [ ] **Step 5: Commit E2E coverage**

```bash
git add e2e Makefile
git commit -m "test: automate the end-to-end grading flow"
```

## Task 6: Write the operator, integration, demo, and limitation documentation

**Files:**
- Create: `README.md`
- Create: `docs/architecture.md`
- Create: `docs/mattermost-setup.md`
- Create: `docs/demo-script.md`
- Create: `docs/test-results.md`
- Create: `docs/limitations.md`
- Create: `docs/api-examples.md`

- [ ] **Step 1: Write README with a tested happy path**

Use exactly these top-level README sections:

```markdown
# Mattermost AI 作业评审系统
## 功能闭环
## 架构概览
## 环境要求
## 五分钟启动
## 演示账号
## Mattermost 接入
## 测试与验收
## AI Provider 切换
## 数据重置
## 当前能力边界
## 目录结构
```

The five-minute path must contain copyable commands from clone through `make demo`, the three URLs, the four demo accounts, and a `make verify` health check. State explicitly that `AI_PROVIDER=mock` is deterministic and `AI_PROVIDER=openai_compatible` requires `AI_BASE_URL`, `AI_API_KEY`, and `AI_MODEL`.

- [ ] **Step 2: Document architecture and Mattermost integration**

`docs/architecture.md` must include:

- container/component diagram;
- publish → submit → aggregate → evaluate → review sequence diagram;
- ownership of auth, assignments, submission versions, jobs, reports, and audit events;
- idempotency keys and report immutability rules;
- failure flow for provider error and invalid output.

`docs/mattermost-setup.md` must identify the outgoing request fields (`token`, `team_id`, `channel_id`, `user_id`, `command`, `text`, `trigger_id`), how the token is verified, how Mattermost users bind to local users, all supported slash-command syntaxes, interactive action callback verification, Bot Token use, and the exact `make demo-full` setup path.

- [ ] **Step 3: Write the timed demonstration script**

`docs/demo-script.md` must use this timed agenda:

```markdown
# 15–20 分钟验收演示脚本
## 00:00–02:00 目标、角色与架构
## 02:00–05:00 教师发布作业
## 05:00–07:00 三名学生提交
## 07:00–10:00 汇总与 Agent 批量评估
## 10:00–14:00 三类报告对比
## 14:00–16:30 教师确认、修改与重新评估
## 16:30–18:00 Mattermost 接入说明
## 18:00–20:00 测试证据与能力边界
## 故障降级路径
```

Each section must list the account to use, page or Mattermost command to open, exact click/command sequence, expected visible state, and one sentence to say aloud. The fallback uses seeded reports when the real provider or network is unavailable.

- [ ] **Step 4: Record truthful results and limitations**

`docs/test-results.md` must contain a table with one row for each of the six required cases, command, expected behavior, actual result, evidence path, and execution timestamp. Populate “actual result” only from a completed verification run.

`docs/limitations.md` must explicitly state:

- AI output may be wrong and always requires teacher confirmation;
- source code is reviewed as text and is not executed in a sandbox;
- vague prompts and incomplete answers reduce confidence;
- real-provider availability, rate limits, cost, and latency are external dependencies;
- Mattermost demo authentication is simplified and production requires HTTPS, rotated secrets, and restricted origins;
- the role model distinguishes paths but is not an institution-scale permission system;
- no plagiarism detection, file OCR, or hidden-test execution is claimed.

`docs/api-examples.md` must provide working curl examples for login, assignment create/publish, submission, summary, evaluation request/status, report confirm/modify/re-evaluate, and all Mattermost adapter commands.

- [ ] **Step 5: Check every documented command**

```bash
make demo
make verify
make demo-full
make mattermost-configure
```

Expected: all copied commands exit `0`. Update documentation immediately if the implemented command or port differs; do not document an untested path.

- [ ] **Step 6: Commit acceptance documentation**

```bash
git add README.md docs/architecture.md docs/mattermost-setup.md docs/demo-script.md docs/test-results.md docs/limitations.md docs/api-examples.md
git commit -m "docs: add complete acceptance and demo guide"
```

## Task 7: Build one verification command and evidence manifest

**Files:**
- Create: `scripts/verify_acceptance.py`
- Create: `scripts/tests/test_verify_acceptance.py`
- Create: `artifacts/acceptance/.gitkeep`
- Modify: `Makefile`

- [ ] **Step 1: Write manifest tests**

Create `scripts/tests/test_verify_acceptance.py`:

```python
from verify_acceptance import build_manifest


def test_manifest_exposes_every_acceptance_gate():
    manifest = build_manifest("2026-07-25T12:00:00+08:00")
    assert set(manifest["checks"]) == {
        "backend_unit",
        "backend_acceptance",
        "frontend_unit",
        "frontend_build",
        "compose_config",
        "api_ready",
        "web_ready",
        "e2e",
    }
    assert manifest["required_scenarios"] == [
        "complete",
        "partial",
        "incorrect",
        "ambiguous",
        "malformed_agent_output",
        "capability_boundary",
    ]
    assert manifest["capability_boundary_checks"] == [
        "code_not_executed",
        "missing_rubric_low_confidence",
        "overlength_rejected",
        "provider_outage_preserves_submission",
        "ambiguous_prompt_human_review",
        "prompt_injection_contained",
    ]
```

- [ ] **Step 2: Implement the verifier**

Create `scripts/verify_acceptance.py` with:

```python
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts" / "acceptance" / "manifest.json"


COMMANDS = {
    "backend_unit": [".venv/bin/pytest", "backend/tests", "--ignore=backend/tests/acceptance", "-q"],
    "backend_acceptance": [".venv/bin/pytest", "backend/tests/acceptance", "-q"],
    "frontend_unit": ["npm", "--prefix", "frontend", "test"],
    "frontend_build": ["npm", "--prefix", "frontend", "run", "build"],
    "compose_config": ["docker", "compose", "config", "--quiet"],
    "api_ready": ["curl", "--fail", "http://localhost:8000/health/ready"],
    "web_ready": ["curl", "--fail", "http://localhost:8080/healthz"],
    "e2e": ["npm", "--prefix", "e2e", "test"],
}


def build_manifest(started_at: str) -> dict[str, object]:
    return {
        "started_at": started_at,
        "checks": {name: {"command": command, "status": "pending"} for name, command in COMMANDS.items()},
        "required_scenarios": [
            "complete",
            "partial",
            "incorrect",
            "ambiguous",
            "malformed_agent_output",
            "capability_boundary",
        ],
        "capability_boundary_checks": [
            "code_not_executed",
            "missing_rubric_low_confidence",
            "overlength_rejected",
            "provider_outage_preserves_submission",
            "ambiguous_prompt_human_review",
            "prompt_injection_contained",
        ],
    }


def main() -> int:
    manifest = build_manifest(datetime.now().astimezone().isoformat())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    for name, command in COMMANDS.items():
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        manifest["checks"][name].update(
            status="passed" if result.returncode == 0 else "failed",
            returncode=result.returncode,
            stdout=result.stdout[-4000:],
            stderr=result.stderr[-4000:],
        )
        OUTPUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        if result.returncode:
            return result.returncode
    manifest["finished_at"] = datetime.now().astimezone().isoformat()
    OUTPUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Add top-level commands**

Add to `Makefile`:

```makefile
demo:
	docker compose up -d --build --wait postgres test-postgres redis api worker web
	docker compose exec api alembic upgrade head
	docker compose exec api python -m scripts.seed_demo

demo-seed:
	docker compose exec api python -m scripts.seed_demo

verify:
	.venv/bin/python scripts/verify_acceptance.py

down:
	docker compose --profile mattermost down
```

- [ ] **Step 4: Run the full evidence pass**

```bash
PYTHONPATH=scripts .venv/bin/pytest scripts/tests -q
make demo
make verify
.venv/bin/python -m json.tool artifacts/acceptance/manifest.json >/dev/null
```

Expected: every manifest check is `passed`, the six scenario names are present, and the manifest has both `started_at` and `finished_at`.

- [ ] **Step 5: Transfer real results into the human-readable table**

Copy only the check status, timestamp, and stable evidence paths from `artifacts/acceptance/manifest.json` into `docs/test-results.md`. Do not commit raw provider prompts, access tokens, student secrets, or large Playwright traces.

- [ ] **Step 6: Commit verification automation**

```bash
git add scripts/verify_acceptance.py scripts/tests Makefile artifacts/acceptance/.gitkeep docs/test-results.md
git commit -m "chore: add reproducible acceptance verification"
```

## Task 8: Run the final acceptance audit and prepare submission

**Files:**
- Modify: `docs/test-results.md`
- Create: `docs/submission-checklist.md`

- [ ] **Step 1: Create a requirement-to-evidence checklist**

Create `docs/submission-checklist.md` with one checked row only after evidence exists for:

1. runnable local system;
2. Mattermost connection method and credentials documented;
3. complete publish → submit → aggregate → evaluate → review flow;
4. three visibly different report examples;
5. all six required test cases;
6. honest capability boundaries;
7. README sufficient for another person to run the system;
8. code stored under the required `projects/<name>/` repository location;
9. progress tracker updated to “已提交”;
10. live presentation or fallback 录屏 arranged with the administrator in advance.

- [ ] **Step 2: Run clean-room startup**

From a fresh clone or disposable worktree, copy only `.env.example` to `.env`, then run:

```bash
make demo
make verify
make demo-full
```

Expected: no untracked local configuration beyond `.env` and generated acceptance artifacts is required. Record the OS, Docker version, commit SHA, start time, and finish time in `docs/test-results.md`.

- [ ] **Step 3: Rehearse against the clock**

Follow `docs/demo-script.md` without improvising setup. Confirm:

- the main flow finishes by minute 16:30;
- tests and limitations finish by minute 20:00;
- seeded fallback reports are reachable in under 30 seconds;
- no secret or private token appears on screen;
- all three teacher actions—confirm, modify, re-evaluate—are demonstrated.

- [ ] **Step 4: Verify repository state and submission path**

```bash
git status --short
git log -1 --oneline
git ls-files | rg '(^README.md$|^docs/|^backend/|^frontend/|^e2e/|^scripts/|docker-compose.yml|Makefile)'
```

Expected: only intentionally ignored `.env` and generated artifacts are absent from version control; all source and required docs are tracked.

- [ ] **Step 5: Perform the external submission steps**

Set `COURSE_PROJECT_DIR` to the exact personal directory name issued by the administrator, verify it is non-empty, then move or merge the finished project into `projects/${COURSE_PROJECT_DIR}/` without renaming that issued directory. Push the verified commit, change the course progress tracker to “已提交”, and notify the administrator before the deadline if a recording must replace live presentation.

- [ ] **Step 6: Commit final evidence**

```bash
git add docs/test-results.md docs/submission-checklist.md
git commit -m "docs: finalize acceptance evidence"
git status --short
```

Expected: final status is clean except ignored local secrets and generated artifacts.
