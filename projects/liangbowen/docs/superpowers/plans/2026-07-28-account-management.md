# Role-Safe Account Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bootstrapped administrator and a secure account-management workflow in which administrators create teachers or students, teachers create students only, and administrators never inherit teacher course-data access.

**Architecture:** Add an immutable account-creation event beside the existing `User` model, then expose bounded list/create operations through a dedicated users service and router. Add one shared React account-management workspace with role-specific routes and creation controls, while keeping local identity independent from Mattermost and tightening existing assignment authorization to explicit roles.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL, Pydantic 2, AnyIO, bcrypt/JWT, React 19, React Router 8, TanStack Query, Zod, Vitest/Testing Library, Playwright, Docker Compose.

---

## File and Responsibility Map

- `backend/app/users/model.py`: existing local account record; no creator or password API fields.
- `backend/app/users/creation_event.py`: immutable account-provisioning evidence model and PostgreSQL trigger DDL.
- `backend/app/users/schemas.py`: validated create request and password-free list/read responses.
- `backend/app/users/service.py`: transactional authorization, password hashing, duplicate handling, creation-event insertion, and visibility-scoped listing.
- `backend/app/users/router.py`: authenticated HTTP list/create contract and safe status mapping.
- `backend/migrations/versions/0008_account_creation_events.py`: production schema and append-only triggers.
- `backend/app/main.py`: users router registration and exact 4 KiB request limit.
- `backend/app/assignments/router.py`: explicit teacher/student authorization with administrator denial.
- `backend/scripts/seed_demo.py`: fixed local administrator and five-user idempotent manifest.
- `frontend/src/users/api.ts`: strict Zod-backed account list/create queries.
- `frontend/src/users/userValidation.ts`: shared client-side canonical username, display-name, and password checks.
- `frontend/src/users/CreateUserDialog.tsx`: accessible, role-aware account creation dialog.
- `frontend/src/users/AccountManagementPage.tsx`: administrator/teacher account register.
- `frontend/src/users/users.css`: responsive editorial-ledger presentation.
- `frontend/src/auth/routing.ts`, `frontend/src/auth/RequireRole.tsx`, `frontend/src/app/router.tsx`: administrator landing and strict route isolation.
- `frontend/src/assignments/TeacherDashboard.tsx`: teacher entry to student management.
- `e2e/tests/account-management.spec.ts`: real administrator → teacher → student workflow and denial checks.

## Task 1: Persist Immutable Account-Creation Evidence

**Files:**
- Create: `backend/app/users/creation_event.py`
- Create: `backend/migrations/versions/0008_account_creation_events.py`
- Create: `backend/tests/migrations/test_account_creation_migration.py`
- Create: `backend/tests/users/__init__.py`
- Create: `backend/tests/users/test_account_creation_model.py`
- Modify: `backend/migrations/env.py`
- Modify: `backend/tests/conftest.py`

- [ ] **Step 1: Write failing model and PostgreSQL trigger tests**

Create tests that require the exact columns, foreign keys, unique target, role enum plus a teacher/student-only check, request-ID bound, and append-only behavior:

```python
def test_account_creation_event_contract() -> None:
    table = Base.metadata.tables["account_creation_events"]
    assert table is AccountCreationEvent.__table__
    assert set(table.c) == {
        table.c.id,
        table.c.actor_user_id,
        table.c.target_user_id,
        table.c.target_role,
        table.c.request_id,
        table.c.created_at,
    }
    assert table.c.target_user_id.unique is True
    assert table.c.target_role.type.enums == ["teacher", "student", "admin"]


async def test_account_creation_events_are_append_only(postgres_session: AsyncSession) -> None:
    actor = make_user(role=Role.ADMIN)
    target = make_user(role=Role.STUDENT)
    postgres_session.add_all([actor, target])
    await postgres_session.flush()
    event = AccountCreationEvent(
        actor_user_id=actor.id,
        target_user_id=target.id,
        target_role=Role.STUDENT,
        request_id="request-1",
    )
    postgres_session.add(event)
    await postgres_session.commit()
    with pytest.raises(DBAPIError):
        await postgres_session.execute(
            update(AccountCreationEvent)
            .where(AccountCreationEvent.id == event.id)
            .values(request_id="request-2")
        )
        await postgres_session.commit()
    await postgres_session.rollback()
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests/users/test_account_creation_model.py -q
```

Expected: collection fails because `app.users.creation_event` does not exist.

- [ ] **Step 3: Implement the model and migration**

Create a focused model with restrictive foreign keys and PostgreSQL-only mutation guards:

```python
class AccountCreationEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "account_creation_events"

    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    target_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    target_role: Mapped[Role] = mapped_column(
        Enum(Role, name="role", values_callable=enum_values),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
```

Attach DDL that creates one trigger function raising SQLSTATE `55000` and three triggers rejecting `UPDATE`, `DELETE`, and `TRUNCATE`. Migration `0008_account_creation_events` must reproduce the same table, indexes, request-ID check (`1..128` bytes, trimmed, no control characters), `target_role IN ('teacher', 'student')`, foreign keys, and triggers, and must drop triggers/function/table in safe downgrade order. `backend/tests/migrations/test_account_creation_migration.py` must upgrade from `0007_retry_generations`, inspect the new table and triggers, verify mutation rejection, downgrade back to `0007_retry_generations`, and confirm the table/function are gone.

Import `AccountCreationEvent` in Alembic metadata loading and the PostgreSQL fixture metadata loading.

- [ ] **Step 4: Run model, migration, and formatting checks**

Run:

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests/users/test_account_creation_model.py backend/tests/migrations -q
.venv/bin/ruff check backend/app/users backend/migrations/versions/0008_account_creation_events.py backend/tests/users
.venv/bin/ruff format --check backend/app/users backend/migrations/versions/0008_account_creation_events.py backend/tests/users
```

Expected: all tests pass and Ruff reports no issues.

- [ ] **Step 5: Commit the persistence contract**

```bash
git add backend/app/users/creation_event.py backend/migrations/versions/0008_account_creation_events.py backend/migrations/env.py backend/tests/conftest.py backend/tests/users backend/tests/migrations/test_account_creation_migration.py
git commit -m "feat: add immutable account creation evidence"
```

## Task 2: Validate and Create Accounts Transactionally

**Files:**
- Create: `backend/app/users/schemas.py`
- Create: `backend/app/users/service.py`
- Create: `backend/tests/users/test_schemas.py`
- Create: `backend/tests/users/test_service.py`

- [ ] **Step 1: Write failing schema boundary tests**

Cover canonical lowercase usernames, trimmed visible display names, allowed target roles, 12–128 character passwords, controls, surrounding whitespace, and username equality:

```python
@pytest.mark.parametrize(
    "username",
    ["ab", "Student4", "-student", "student space", "student/4", "x" * 65],
)
def test_user_create_rejects_noncanonical_usernames(username: str) -> None:
    with pytest.raises(ValidationError):
        UserCreate(
            username=username,
            display_name="张三",
            role=Role.STUDENT,
            password="Course2026!Secure",
        )


@pytest.mark.parametrize("role", [Role.ADMIN, Role.TEACHER, Role.STUDENT])
def test_user_create_recognizes_every_role_for_explicit_authorization(role: Role) -> None:
    request = UserCreate(
        username="account.001",
        display_name=" 新账号 ",
        role=role,
        password="Course2026!Secure",
    )
    assert request.display_name == "新账号"
```

- [ ] **Step 2: Write failing service permission and atomicity tests**

Require this behavior:

```python
@pytest.mark.parametrize(
    ("actor_role", "target_role", "allowed"),
    [
        (Role.ADMIN, Role.TEACHER, True),
        (Role.ADMIN, Role.STUDENT, True),
        (Role.TEACHER, Role.STUDENT, True),
        (Role.ADMIN, Role.ADMIN, False),
        (Role.TEACHER, Role.TEACHER, False),
        (Role.TEACHER, Role.ADMIN, False),
        (Role.STUDENT, Role.STUDENT, False),
    ],
)
async def test_create_user_permission_matrix(
    postgres_session: AsyncSession,
    actor_role: Role,
    target_role: Role,
    allowed: bool,
) -> None:
    actor = make_user(role=actor_role)
    postgres_session.add(actor)
    await postgres_session.commit()
    request = user_create(role=target_role)
    if not allowed:
        with pytest.raises(AccountRoleForbidden):
            await create_user(postgres_session, actor.id, request, "request-1")
        return
    created = await create_user(postgres_session, actor.id, request, "request-1")
    stored = await postgres_session.get(User, created.id)
    assert stored is not None
    assert verify_password(request.password, stored.password_hash)
    event = await postgres_session.scalar(
        select(AccountCreationEvent).where(AccountCreationEvent.target_user_id == created.id)
    )
    assert event is not None and event.actor_user_id == actor.id
```

Also test inactive actors, concurrent duplicate username inserts, audit insertion failure rollback, deterministic listing order, administrator visibility of all roles, teacher visibility of students only, and absence of password fields in all response schemas.

- [ ] **Step 3: Run focused tests and verify RED**

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests/users/test_schemas.py backend/tests/users/test_service.py -q
```

Expected: imports fail because schemas and service are not implemented.

- [ ] **Step 4: Implement schemas and service**

Define the public contract with `ConfigDict(from_attributes=True)` and no secret fields:

```python
USERNAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,63}$"


class UserCreate(BaseModel):
    username: Annotated[str, StringConstraints(strip_whitespace=True, pattern=USERNAME_PATTERN)]
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    role: Role
    password: Annotated[str, Field(min_length=12, max_length=128)]

    @model_validator(mode="after")
    def validate_visible_and_secret_text(self) -> Self:
        if any(unicodedata.category(char).startswith("C") for char in self.display_name):
            raise ValueError("display name contains control characters")
        if self.password != self.password.strip():
            raise ValueError("password must not have surrounding whitespace")
        if any(unicodedata.category(char).startswith("C") for char in self.password):
            raise ValueError("password contains control characters")
        if self.password == self.username:
            raise ValueError("password must differ from username")
        return self
```

Implement `create_user(session, actor_id, request, request_id)` so it:

1. reselects and locks an active administrator or teacher actor;
2. raises `AccountRoleForbidden` unless the matrix allows the target role;
3. hashes the password through `anyio.to_thread.run_sync` with a module `CapacityLimiter(4)`;
4. inserts `User` and `AccountCreationEvent` in one transaction;
5. maps only PostgreSQL constraint `ix_users_username` to `UsernameAlreadyExists`;
6. rolls back all other exceptions and never logs request content;
7. returns the created password-free `UserAccountRead` assembled with the actor summary.

Implement `list_users(session, actor_id, limit, offset)` with an active-actor recheck, role-scoped filters, a count query, left joins through `AccountCreationEvent` to an aliased creator `User`, and ordering `User.created_at.desc(), User.id.desc()`.

- [ ] **Step 5: Verify schemas, service, concurrency, and lint**

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests/users/test_schemas.py backend/tests/users/test_service.py -q
.venv/bin/ruff check backend/app/users backend/tests/users
.venv/bin/ruff format --check backend/app/users backend/tests/users
```

Expected: all focused tests pass, including exactly one committed user/event under concurrent duplicate creation.

- [ ] **Step 6: Commit the account service**

```bash
git add backend/app/users/schemas.py backend/app/users/service.py backend/tests/users
git commit -m "feat: add role-safe account provisioning service"
```

## Task 3: Expose the Protected Users API

**Files:**
- Create: `backend/app/users/router.py`
- Create: `backend/tests/users/test_router.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing HTTP contract tests**

Build real FastAPI requests with tokens for every role:

```python
async def test_admin_can_create_teacher(client: AsyncClient, users: dict[str, User]) -> None:
    response = await client.post(
        "/api/v1/users",
        headers=bearer(users["admin"]),
        json={
            "username": "teacher2",
            "display_name": "教师二",
            "role": "teacher",
            "password": "Course2026!Secure",
        },
    )
    assert response.status_code == 201
    assert response.json()["username"] == "teacher2"
    assert "password" not in response.text


async def test_teacher_cannot_create_teacher(client: AsyncClient, users: dict[str, User]) -> None:
    response = await client.post(
        "/api/v1/users",
        headers=bearer(users["teacher"]),
        json={
            "username": "teacher2",
            "display_name": "教师二",
            "role": "teacher",
            "password": "Course2026!Secure",
        },
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role for requested account"}
```

Add tests for teacher-created students, administrator/teacher attempts to create an administrator receiving `403`, student denial, unauthenticated `401`, duplicate `409`, invalid `422`, list visibility/pagination, no password leakage, request ID recorded from middleware, and a body larger than 4 KiB receiving `413` before JSON parsing.

- [ ] **Step 2: Run HTTP tests and verify RED**

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests/users/test_router.py -q
```

Expected: `/api/v1/users` returns `404` and the account body-limit assertion fails.

- [ ] **Step 3: Implement router and main-app registration**

Implement explicit role dependencies and safe exception mapping:

```python
router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=UserAccountPage)
async def list_accounts(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.ADMIN, Role.TEACHER))],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> UserAccountPage:
    return await list_users(session, current_user.id, limit=limit, offset=offset)


@router.post("", response_model=UserAccountRead, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: UserCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.ADMIN, Role.TEACHER))],
) -> UserAccountRead:
    try:
        return await create_user(session, current_user.id, payload, str(request.state.request_id))
    except AccountRoleForbidden:
        raise HTTPException(status_code=403, detail="insufficient role for requested account") from None
    except UsernameAlreadyExists:
        raise HTTPException(status_code=409, detail="username already exists") from None
```

Register the router under `/api/v1`. Add `ACCOUNT_CREATE_PATH = "/api/v1/users"` and `MAX_ACCOUNT_REQUEST_BYTES = 4 * 1024` to `SubmissionBodyLimitMiddleware._request_limit` for `POST` only.

- [ ] **Step 4: Run focused and auth regression tests**

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests/users backend/tests/auth -q
.venv/bin/ruff check backend/app/main.py backend/app/users backend/tests/users
```

Expected: all tests pass and unauthorized responses retain the existing generic bearer challenge.

- [ ] **Step 5: Commit the users API**

```bash
git add backend/app/main.py backend/app/users/router.py backend/tests/users/test_router.py
git commit -m "feat: expose protected account management api"
```

## Task 4: Close Administrator Course-Data Authorization Gaps

**Files:**
- Modify: `backend/app/assignments/router.py`
- Modify: `backend/tests/assignments/test_api.py`

- [ ] **Step 1: Write failing administrator-denial tests**

Require `403` for administrator list and detail requests while preserving teacher/student behavior:

```python
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/assignments"),
        ("GET", f"/api/v1/assignments/{ASSIGNMENT_ID}"),
        ("GET", f"/api/v1/assignments/{ASSIGNMENT_ID}/summary"),
    ],
)
async def test_admin_cannot_read_course_data(
    client: AsyncClient,
    admin_token: str,
    method: str,
    path: str,
) -> None:
    response = await client.request(method, path, headers=bearer_token(admin_token))
    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role"}
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests/assignments/test_api.py -q
```

Expected: administrator list/detail cases return `200` instead of `403`.

- [ ] **Step 3: Make every assignment route role-explicit**

Use `require_roles(Role.TEACHER, Role.STUDENT)` for shared reads, preserve `require_roles(Role.TEACHER)` for teacher-only writes/summary, and branch only after the dependency has rejected administrators:

```python
current_user: Annotated[
    User,
    Depends(require_roles(Role.TEACHER, Role.STUDENT)),
]
```

Do not grant administrators access to evaluation, review, or submission teacher routes. Audit those routers with `rg` and add a regression assertion for every route that uses a broad `get_current_user` dependency.

- [ ] **Step 4: Run assignment, evaluation, review, and submission route tests**

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests/assignments backend/tests/evaluations backend/tests/reviews backend/tests/submissions -q
.venv/bin/ruff check backend/app/assignments backend/tests/assignments
```

Expected: all course-flow tests pass and every administrator course-data case is `403`.

- [ ] **Step 5: Commit the authorization fix**

```bash
git add backend/app/assignments/router.py backend/tests/assignments
git commit -m "fix: keep administrators outside course data"
```

## Task 5: Bootstrap the Local Administrator Safely

**Files:**
- Modify: `backend/scripts/seed_demo.py`
- Modify: `backend/tests/scripts/test_seed_demo.py`
- Modify: `README.md`
- Modify: `docs/limitations.md`

- [ ] **Step 1: Write failing five-user seed tests**

Require a fixed administrator ID, role, active state, password, idempotent reseeding, five-user summary, and no Mattermost identity:

```python
async def test_seed_contains_local_admin(postgres_session: AsyncSession) -> None:
    summary = await seed_demo(postgres_session)
    admin = await postgres_session.scalar(select(User).where(User.username == "admin"))
    assert summary.users == 5
    assert admin is not None
    assert admin.display_name == "系统管理员"
    assert admin.role is Role.ADMIN
    assert admin.is_active is True
    assert verify_password("Admin123!Secure", admin.password_hash)
```

Update existing exact-manifest expectations from four to five while keeping the three grading scenarios unchanged.

- [ ] **Step 2: Run seed tests and verify RED**

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests/scripts/test_seed_demo.py backend/tests/demo -q
```

Expected: administrator lookup is absent and the summary remains four.

- [ ] **Step 3: Add the administrator manifest**

Add deterministic identity and credentials:

```python
DEMO_USER_IDS = {
    "admin": uuid.UUID("00000000-0000-4000-8000-000000000005"),
    "teacher": uuid.UUID("00000000-0000-4000-8000-000000000001"),
    "student1": uuid.UUID("00000000-0000-4000-8000-000000000002"),
    "student2": uuid.UUID("00000000-0000-4000-8000-000000000003"),
    "student3": uuid.UUID("00000000-0000-4000-8000-000000000004"),
}

DEMO_USERS = (
    ("admin", "系统管理员", Role.ADMIN, "Admin123!Secure"),
    ("teacher", "演示教师", Role.TEACHER, "Teacher123!"),
    ("student1", "学生甲", Role.STUDENT, "Student123!"),
    ("student2", "学生乙", Role.STUDENT, "Student123!"),
    ("student3", "学生丙", Role.STUDENT, "Student123!"),
)
```

Return `users=len(DEMO_USERS)` instead of another hard-coded count. Keep Mattermost desired users and demo bindings restricted to the existing teacher and three students. Document the local administrator credential, its separation from Mattermost's server administrator, the global-student-pool behavior, and the absence of password reset/deactivation in this version.

- [ ] **Step 4: Verify seed, Mattermost configuration, and docs**

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests/scripts/test_seed_demo.py backend/tests/demo backend/tests/mattermost scripts/tests/test_configure_mattermost.py -q
.venv/bin/ruff check backend/scripts/seed_demo.py backend/tests/scripts/test_seed_demo.py
```

Expected: five local accounts, unchanged four Mattermost demo identities, and all tests pass.

- [ ] **Step 5: Commit the bootstrap update**

```bash
git add backend/scripts/seed_demo.py backend/tests/scripts/test_seed_demo.py README.md docs/limitations.md
git commit -m "feat: bootstrap local administrator account"
```

## Task 6: Add Frontend Contracts, Queries, and Role Routing

**Files:**
- Create: `frontend/src/users/api.ts`
- Create: `frontend/src/users/api.test.tsx`
- Modify: `frontend/src/shared/api/schemas.ts`
- Modify: `frontend/src/auth/routing.ts`
- Modify: `frontend/src/auth/RequireRole.tsx`
- Modify: `frontend/src/auth/auth.test.tsx`

- [ ] **Step 1: Write failing schema, query, and routing tests**

Require strict password-free responses and role landings:

```tsx
expect(roleLanding("admin")).toBe("/admin/users");
expect(safePostLoginDestination("/admin/users", "admin")).toBe("/admin/users");
expect(safePostLoginDestination("/teacher", "admin")).toBe("/admin/users");

expect(() => userAccountSchema.parse({
  id: ids.user,
  username: "student4",
  display_name: "张三",
  role: "student",
  is_active: true,
  created_at: "2026-07-28T12:00:00Z",
  created_by: null,
  password_hash: "forbidden",
})).toThrow();
```

Mock `/users` to verify `useUsers` caching is keyed by actor/offset and `useCreateUser` invalidates only the current actor's account list. Assert that create payloads are sent only in the request and never become mutation results.

- [ ] **Step 2: Run focused frontend tests and verify RED**

```bash
npm --prefix frontend test -- src/auth/auth.test.tsx src/users/api.test.tsx
```

Expected: missing user schemas/module and `admin` landing assertion failures.

- [ ] **Step 3: Implement contracts, hooks, and generalized guards**

Add strict schemas:

```tsx
export const accountCreatorSchema = z.object({
  id: uuidSchema,
  username: z.string(),
  display_name: z.string(),
}).strict();

export const userAccountSchema = z.object({
  id: uuidSchema,
  username: z.string(),
  display_name: z.string(),
  role: roleSchema,
  is_active: z.boolean(),
  created_at: dateTimeSchema,
  created_by: accountCreatorSchema.nullable(),
}).strict();

export const userAccountPageSchema = z.object({
  items: z.array(userAccountSchema),
  total: z.int().nonnegative(),
  limit: z.int().min(1).max(100),
  offset: z.int().min(0).max(10_000),
}).strict();
```

Implement `useUsers(actorId, offset)` and `useCreateUser(actorId)` with TanStack Query and the existing `api` client.

Change `WorkspaceRole` to include `admin`, add exact safe-route expressions for `/admin/users` and `/teacher/users`, and generalize `RequireRole` to accept `role: WorkspaceRole | readonly WorkspaceRole[]` while preserving the current denial UI. Route registration waits for Task 7, when the real account-management page exists; Task 6 tests the pure landing/safe-destination functions and guard component directly.

- [ ] **Step 4: Verify routing and API contracts**

```bash
npm --prefix frontend test -- src/auth/auth.test.tsx src/users/api.test.tsx
npm --prefix frontend run lint
```

Expected: administrator lands only in `/admin/users`, existing teacher/student destinations remain safe, and schemas reject secret fields.

- [ ] **Step 5: Commit frontend account foundations**

```bash
git add frontend/src/shared/api/schemas.ts frontend/src/users/api.ts frontend/src/users/api.test.tsx frontend/src/auth/routing.ts frontend/src/auth/RequireRole.tsx frontend/src/auth/auth.test.tsx
git commit -m "feat: add account management frontend contracts"
```

## Task 7: Build the Shared Account-Management Workspace

**Files:**
- Create: `frontend/src/users/userValidation.ts`
- Create: `frontend/src/users/CreateUserDialog.tsx`
- Create: `frontend/src/users/AccountManagementPage.tsx`
- Create: `frontend/src/users/users.css`
- Create: `frontend/src/users/userValidation.test.ts`
- Create: `frontend/src/users/AccountManagementPage.test.tsx`
- Modify: `frontend/src/app/router.tsx`
- Modify: `frontend/src/assignments/TeacherDashboard.tsx`
- Modify: `frontend/src/assignments/TeacherDashboard.test.tsx`
- Modify: `frontend/src/styles/responsive.css`

- [ ] **Step 1: Write failing validation and page tests**

Cover both page modes and the complete dialog lifecycle:

```tsx
it("lets an administrator select teacher or student", async () => {
  renderApp(["/admin/users"], adminSession);
  await user.click(await screen.findByRole("button", { name: "创建账号" }));
  expect(screen.getByRole("combobox", { name: "账号角色" })).toHaveValue("student");
  expect(screen.getByRole("option", { name: "教师" })).toBeInTheDocument();
});

it("fixes teacher-created accounts to student", async () => {
  renderApp(["/teacher/users"], teacherSession);
  await user.click(await screen.findByRole("button", { name: "创建学生账号" }));
  expect(screen.queryByRole("combobox", { name: "账号角色" })).not.toBeInTheDocument();
  expect(screen.getByText("学生账号", { selector: "strong" })).toBeInTheDocument();
});

it("clears secret fields and refreshes after success", async () => {
  server.use(successfulCreateHandler);
  renderApp(["/teacher/users"], teacherSession);
  await openAndFillValidStudent();
  await user.click(screen.getByRole("button", { name: "创建学生账号" }));
  expect(await screen.findByText("student4 已创建。" )).toBeInTheDocument();
  expect(screen.queryByLabelText("初始密码")).not.toBeInTheDocument();
  expect(await screen.findByText("@student4")).toBeInTheDocument();
});
```

Add tests for canonical username errors, visible display name, 12-character password, password confirmation, password=username, duplicate `409`, validation `422`, network/timeout/contract/500 messages, double-submit prevention, escape/dirty discard confirmation, focus trap/restoration, list loading/empty/error/pagination, administrator role totals, and the teacher dashboard entry link.

- [ ] **Step 2: Run focused UI tests and verify RED**

```bash
npm --prefix frontend test -- src/users/userValidation.test.ts src/users/AccountManagementPage.test.tsx src/assignments/TeacherDashboard.test.tsx
```

Expected: user components and validation module do not exist.

- [ ] **Step 3: Implement deterministic client validation**

Export constants matching the server and a single pure validator:

```tsx
export const USERNAME_PATTERN = /^[a-z0-9][a-z0-9._-]{2,63}$/u;
export const INITIAL_PASSWORD_MIN = 12;
export const INITIAL_PASSWORD_MAX = 128;

export function validateUserDraft(draft: UserDraft): UserFieldErrors {
  const errors: UserFieldErrors = {};
  if (!USERNAME_PATTERN.test(draft.username)) errors.username = "用户名需为 3–64 位小写字母、数字、点、下划线或连字符。";
  if (!hasVisibleText(draft.displayName) || [...draft.displayName.trim()].length > 128) errors.displayName = "显示姓名需为 1–128 个可见字符。";
  if (draft.password !== draft.password.trim() || [...draft.password].length < 12 || [...draft.password].length > 128 || hasControl(draft.password)) errors.password = "初始密码需为 12–128 个字符，且不能包含首尾空格或控制字符。";
  if (draft.password === draft.username) errors.password = "初始密码不能与用户名相同。";
  if (draft.confirmPassword !== draft.password) errors.confirmPassword = "两次输入的密码不一致。";
  return errors;
}
```

- [ ] **Step 4: Implement the dialog and page**

`CreateUserDialog` must use local component state for both password fields, never place passwords in URL/navigation/query data, and clear all local fields before calling `onCreated`. Reuse the assignment dialog's focus trap, body scroll lock, escape behavior, dirty-form navigation blocker, discard confirmation, and focus restoration.

`AccountManagementPage` derives mode from the authenticated role, loads `useUsers`, renders metrics and an accessible table/card register, maps safe errors to Chinese messages, and exposes bounded previous/next pagination. Its table renders:

```tsx
<th scope="row">
  <strong>{account.display_name || account.username}</strong>
  <span>@{account.username}</span>
</th>
<td>{account.role === "admin" ? "管理员" : account.role === "teacher" ? "教师" : "学生"}</td>
<td>{account.created_by?.display_name ?? "系统初始化"}</td>
```

Register the real `AccountManagementPage` at `/admin/users` and `/teacher/users`, add “管理学生账号” to the teacher dashboard, and create responsive styles that preserve the existing warm paper, hard rules, blue actions, visible focus, 44 px touch targets, and compact mobile cards without reintroducing the removed workspace margin line.

- [ ] **Step 5: Verify UI behavior, accessibility, and production build**

```bash
npm --prefix frontend test -- src/users src/assignments/TeacherDashboard.test.tsx src/auth/auth.test.tsx src/app/accessibility.test.tsx
npm --prefix frontend run lint
npm --prefix frontend run build
```

Expected: focused tests pass, ESLint has zero warnings, and Vite produces the production bundle with no errors.

- [ ] **Step 6: Commit the account-management UI**

```bash
git add frontend/src/users frontend/src/app/router.tsx frontend/src/assignments/TeacherDashboard.tsx frontend/src/assignments/TeacherDashboard.test.tsx frontend/src/styles/responsive.css
git commit -m "feat: add role-aware account management workspace"
```

## Task 8: Prove Cross-Feature Semantics and Security Boundaries

**Files:**
- Modify: `backend/tests/assignments/test_summary.py`
- Modify: `backend/tests/auth/test_auth_api.py`
- Modify: `backend/tests/mattermost/test_demo_bindings.py`
- Modify: `frontend/src/app/accessibility.test.tsx`
- Modify: `frontend/src/test/server.ts`
- Modify: `frontend/src/test/fixtures.ts`

- [ ] **Step 1: Add failing cross-feature regression tests**

Add exact assertions that:

- creating one active student increases an existing assignment's `total_students` and `missing_students` by one;
- a token issued before an actor is deactivated or demoted cannot continue creating accounts because database state wins over the JWT role claim;
- local account creation produces no `MattermostIdentity` row;
- explicit Mattermost binding still accepts new teachers/students and still rejects administrators;
- account-management routes have accessible names, labeled controls, live success/error announcements, table headers, keyboard focus, and role-correct navigation.

Representative stale-token assertion:

```python
token = create_access_token(teacher.id, Role.TEACHER)
teacher.role = Role.STUDENT
await session.commit()
response = await client.post("/api/v1/users", headers=bearer_token(token), json=student_payload())
assert response.status_code == 403
```

- [ ] **Step 2: Run the cross-feature tests and verify failures identify missing fixtures or behavior**

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests/assignments/test_summary.py backend/tests/auth/test_auth_api.py backend/tests/mattermost/test_demo_bindings.py -q
npm --prefix frontend test -- src/app/accessibility.test.tsx src/users
```

Expected: any unimplemented fixture/contract boundary fails explicitly; no unrelated test is weakened or skipped.

- [ ] **Step 3: Complete only the required integration adjustments**

Update MSW fixtures with password-free account pages, ensure the service always reads current actor state, leave Mattermost provisioning untouched, and document the global active-student semantics in the account page helper copy and `docs/limitations.md`.

- [ ] **Step 4: Run the complete backend and frontend suites**

```bash
make test-backend
make lint-backend
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

Expected: all backend and frontend tests pass, Ruff/ESLint report zero issues, and both builds complete.

- [ ] **Step 5: Commit integration coverage**

```bash
git add backend/tests frontend/src/test frontend/src/app/accessibility.test.tsx docs/limitations.md
git commit -m "test: cover account management security boundaries"
```

## Task 9: Add End-to-End Acceptance Coverage

**Files:**
- Create: `e2e/tests/account-management.spec.ts`
- Modify: `scripts/verify_acceptance.py`
- Modify: `docs/test-results.md`

- [ ] **Step 1: Write the failing Playwright acceptance flow**

Use a per-run username suffix to avoid collisions and prove the real login chain. Define the helpers in the same file so the test has no hidden dependency:

```tsx
import { expect, type APIRequestContext, type Page, test } from "@playwright/test";

async function login(page: Page, username: string, password: string) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill(username);
  await page.getByRole("textbox", { name: "密码", exact: true }).fill(password);
  await page.getByRole("button", { name: "登录并进入工作台" }).click();
}

async function tokenFor(request: APIRequestContext, username: string, password: string) {
  const response = await request.post("/api/v1/auth/login", {
    data: { username, password },
  });
  expect(response.ok()).toBe(true);
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
  await page.getByRole("button", { name: /确认创建/u }).click();
  await expect(page.getByText(`${account.username} 已创建。`)).toBeVisible();
}

test("administrator creates teacher and teacher creates student", async ({ browser, request }) => {
  const suffix = Date.now().toString(36);
  const teacherUsername = `teacher-${suffix}`;
  const studentUsername = `student-${suffix}`;

  const adminContext = await browser.newContext();
  const adminPage = await adminContext.newPage();
  await login(adminPage, "admin", "Admin123!Secure");
  await createAccount(adminPage, {
    username: teacherUsername,
    displayName: "验收教师",
    role: "teacher",
    password: "Course2026!Secure",
  });

  const teacherContext = await browser.newContext();
  const teacherPage = await teacherContext.newPage();
  await login(teacherPage, teacherUsername, "Course2026!Secure");
  await expect(teacherPage).toHaveURL(/\/teacher$/u);
  await teacherPage.getByRole("link", { name: "管理学生账号" }).click();
  await createAccount(teacherPage, {
    username: studentUsername,
    displayName: "验收学生",
    password: "Student2026!Secure",
  });

  const studentContext = await browser.newContext();
  const studentPage = await studentContext.newPage();
  await login(studentPage, studentUsername, "Student2026!Secure");
  await expect(studentPage).toHaveURL(/\/student$/u);
  await expect(studentPage.getByText("图的最短路径")).toBeVisible();

  const studentToken = await tokenFor(request, studentUsername, "Student2026!Secure");
  const forbidden = await request.get("/api/v1/users", {
    headers: { Authorization: `Bearer ${studentToken}` },
  });
  expect(forbidden.status()).toBe(403);

  await Promise.all([adminContext.close(), teacherContext.close(), studentContext.close()]);
});
```

Add a second test asserting the administrator receives `403` from assignment list/detail and cannot navigate to teacher pages.

- [ ] **Step 2: Run Playwright and verify RED**

```bash
npm --prefix e2e run test:playwright -- account-management.spec.ts
```

Expected: the flow fails before the rebuilt seeded stack contains the new administrator/UI.

- [ ] **Step 3: Register the test in strict acceptance evidence**

Update the verifier's expected Playwright manifest and test counts without weakening clean-room, PID-cleanup, or external-dependency evidence rules. Update `docs/test-results.md` only from fresh command output.

- [ ] **Step 4: Rebuild the disposable stack and run acceptance**

```bash
docker compose -p newproject --env-file .env up -d --build --force-recreate --wait
npm --prefix e2e run test:playwright -- account-management.spec.ts
python3 scripts/verify_acceptance.py --local-only
```

Expected: administrator → teacher → student flow passes, denial checks pass, and the local acceptance verifier records a successful run.

- [ ] **Step 5: Commit end-to-end evidence changes**

```bash
git add e2e/tests/account-management.spec.ts scripts/verify_acceptance.py docs/test-results.md
git commit -m "test: verify account provisioning end to end"
```

## Task 10: Final Documentation, Visual QA, and Delivery Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/api-examples.md`
- Modify: `docs/limitations.md`
- Modify: `docs/test-results.md`

- [ ] **Step 1: Update operator and demonstration instructions**

Document:

- `admin / Admin123!Secure` as a local development/test credential only;
- administrator and teacher creation permissions;
- exact web routes and API examples;
- password policy and duplicate behavior;
- absence of edit/delete/reset/deactivate/self-registration;
- immediate global assignment-population impact of active students;
- separation from Mattermost accounts and the explicit binding requirement;
- administrator denial from course data.

API example:

```bash
curl -fsS http://127.0.0.1:8000/api/v1/users \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"username":"student4","display_name":"张三","role":"student","password":"Course2026!Secure"}'
```

The docs must warn users not to place real production secrets in shell history and must not include a real token or external API credential.

- [ ] **Step 2: Run documentation and repository hygiene checks**

```bash
rg -n "admin|账号管理|Mattermost|密码重置|全局.*学生" README.md docs/api-examples.md docs/limitations.md
git diff --check
git status --short
```

Expected: all required boundaries are documented, no whitespace errors, and only intended files are modified.

- [ ] **Step 3: Perform browser visual and keyboard QA**

Using the local rebuilt app:

1. sign in as administrator and inspect `/admin/users` at desktop and mobile widths;
2. create a teacher and confirm focus returns to the trigger and the success region is announced;
3. sign in as that teacher, open `/teacher/users`, and verify there is no role selector;
4. verify keyboard tab order, escape/discard protection, visible focus, long username/display-name wrapping, empty/list/error states, and touch target sizing;
5. verify the removed red workspace margin line remains absent.

- [ ] **Step 4: Run fresh full verification before completion**

```bash
make verify-local
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
curl --noproxy '*' -fsS http://127.0.0.1:8080/healthz
docker compose -p newproject --env-file .env ps
git status --short
```

Expected: strict local acceptance succeeds, frontend tests/lint/build pass, Web and API are healthy, Compose services are healthy, and the worktree is clean after the final commit.

- [ ] **Step 5: Commit final documentation**

```bash
git add README.md docs/api-examples.md docs/limitations.md docs/test-results.md
git commit -m "docs: document account management workflow"
```

## Completion Criteria

- The seeded local administrator can sign in and create teachers and students.
- A newly created teacher can sign in and create students, but cannot create teachers or administrators.
- A student cannot reach or call account management.
- No application caller can create an administrator.
- Passwords and hashes are absent from responses, logs, audit events, browser storage, query caches, and URLs.
- Concurrent duplicate creation commits exactly one account and event.
- Creation events are immutable and atomically tied to account creation.
- Administrators receive `403` from assignment, evaluation, submission, and review teacher data.
- New local accounts are not automatically bound to Mattermost.
- A new active student immediately participates in the single-course assignment population.
- Backend, frontend, end-to-end, migration, lint, build, health, accessibility, and visual checks all pass with fresh evidence.
