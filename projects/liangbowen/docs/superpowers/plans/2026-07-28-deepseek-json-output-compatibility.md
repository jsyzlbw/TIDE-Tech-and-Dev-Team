# DeepSeek JSON Output Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing OpenAI-compatible grading provider work with DeepSeek JSON Output while preserving strict JSON Schema behavior as the default.

**Architecture:** Add an explicit typed response-format setting that flows from environment configuration through the provider factory. The provider keeps its current strict `json_schema` payload by default and, for `json-object`, sends DeepSeek's supported format plus the grading schema as a trusted system instruction; existing bounded decoding and evaluation validation remain unchanged.

**Tech Stack:** Python 3.12, Pydantic Settings, httpx, pytest, Docker Compose, Markdown

---

## File map

- Modify `backend/app/core/config.py`: typed `agent_response_format` setting.
- Modify `backend/app/evaluations/providers/factory.py`: forward the mode.
- Modify `backend/app/evaluations/providers/openai_compatible.py`: validate the mode and build the correct request.
- Modify `backend/tests/evaluations/test_jobs.py`: settings and factory tests.
- Modify `backend/tests/evaluations/test_providers.py`: request-body tests.
- Modify `docker-compose.yml`, `.env.example`, and `README.md`: runtime propagation and documentation.
- Modify `backend/tests/ops/test_runtime_contract.py`: configuration contract.
- Modify ignored local `.env`: activate DeepSeek without storing the key value.

### Task 1: Typed configuration and factory propagation

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/evaluations/providers/factory.py`
- Modify: `backend/app/evaluations/providers/openai_compatible.py`
- Test: `backend/tests/evaluations/test_jobs.py`

- [ ] **Step 1: Write the failing settings and factory test**

```python
def test_provider_response_format_is_typed_and_forwarded() -> None:
    with pytest.raises(ValueError):
        Settings(agent_response_format="xml")  # type: ignore[arg-type]

    settings = Settings(
        app_env="test",
        jwt_secret="test-only",
        agent_provider="openai-compatible",
        agent_api_key="top-secret-api-key",
        agent_response_format="json-object",
    )
    provider = create_evaluation_provider(settings)
    assert provider.response_format == "json-object"
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
PYTHONPATH=backend .venv/bin/pytest -q backend/tests/evaluations/test_jobs.py -k response_format
```

Expected: failure because settings and provider do not expose the response-format contract.

- [ ] **Step 3: Add the minimal typed setting and forwarding**

Add to `Settings`:

```python
agent_response_format: Literal["json-schema", "json-object"] = "json-schema"
```

Pass it through `create_evaluation_provider`. Store it in the provider after an exact runtime type/value check and expose:

```python
@property
def response_format(self) -> Literal["json-schema", "json-object"]:
    return self._response_format
```

Invalid constructor values raise `ValueError("response_format is invalid")`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command. Expected: selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/config.py backend/app/evaluations/providers/factory.py backend/app/evaluations/providers/openai_compatible.py backend/tests/evaluations/test_jobs.py
git commit -m "feat: configure agent response format"
```

### Task 2: DeepSeek-compatible JSON Object request

**Files:**
- Modify: `backend/app/evaluations/providers/openai_compatible.py`
- Test: `backend/tests/evaluations/test_providers.py`

- [ ] **Step 1: Write a failing JSON-object request test**

Construct the provider with `response_format="json-object"`, capture the request, and assert:

```python
assert payload["response_format"] == {"type": "json_object"}
assert "json_schema" not in payload["response_format"]
assert "evaluation_output_v1" in payload["messages"][0]["content"]
assert '"required"' in payload["messages"][0]["content"]
```

Keep the existing strict-schema test unchanged to prove the default behavior.

- [ ] **Step 2: Run provider tests and verify RED**

```bash
PYTHONPATH=backend .venv/bin/pytest -q backend/tests/evaluations/test_providers.py -k 'strict_schema or json_object'
```

Expected: the new test fails because every request still emits `json_schema`.

- [ ] **Step 3: Implement the conditional request body**

For `json-schema`, return the existing body. For `json-object`, use:

```python
{"type": "json_object"}
```

Append the exact grading schema to the trusted system instruction only in JSON-object mode:

```python
schema_json = json.dumps(
    evaluation_output_provider_schema(),
    ensure_ascii=False,
    allow_nan=False,
    separators=(",", ":"),
)
system_content = f"{_SYSTEM_PROMPT} JSON schema evaluation_output_v1: {schema_json}"
```

The assignment, rubric, and answer remain exclusively in the untrusted user data message.

- [ ] **Step 4: Run tests and verify GREEN**

```bash
PYTHONPATH=backend .venv/bin/pytest -q backend/tests/evaluations/test_providers.py backend/tests/evaluations/test_jobs.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/evaluations/providers/openai_compatible.py backend/tests/evaluations/test_providers.py
git commit -m "feat: support JSON object grading providers"
```

### Task 3: Runtime configuration and documentation

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `README.md`
- Test: `backend/tests/ops/test_runtime_contract.py`

- [ ] **Step 1: Write failing runtime-contract assertions**

```python
assert "AGENT_RESPONSE_FORMAT=json-schema" in example
for service in (api, worker, beat):
    assert "AGENT_RESPONSE_FORMAT: ${AGENT_RESPONSE_FORMAT:-json-schema}" in service
```

- [ ] **Step 2: Run the contract test and verify RED**

```bash
PYTHONPATH=backend .venv/bin/pytest -q backend/tests/ops/test_runtime_contract.py -k agent
```

Expected: failure because the variable is absent.

- [ ] **Step 3: Propagate and document the setting**

Add to API, worker, and beat:

```yaml
AGENT_RESPONSE_FORMAT: ${AGENT_RESPONSE_FORMAT:-json-schema}
```

Add `AGENT_RESPONSE_FORMAT=json-schema` to `.env.example`. Extend README with a DeepSeek example using `deepseek-v4-flash`, `https://api.deepseek.com`, and `json-object`; explain the `DEEPSEEK_API_KEY` reference and service recreation.

- [ ] **Step 4: Run contract and formatting checks**

```bash
PYTHONPATH=backend .venv/bin/pytest -q backend/tests/ops/test_runtime_contract.py backend/tests/ops/test_compose_contract.py
.venv/bin/ruff check backend/app/core/config.py backend/app/evaluations/providers backend/tests/evaluations backend/tests/ops
.venv/bin/ruff format --check backend/app/core/config.py backend/app/evaluations/providers backend/tests/evaluations backend/tests/ops
docker compose config --quiet
```

Expected: all tests and checks pass; Compose emits no error.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml .env.example README.md backend/tests/ops/test_runtime_contract.py
git commit -m "docs: configure DeepSeek grading runtime"
```

### Task 4: Activate and verify the real DeepSeek provider

**Files:**
- Modify locally only: ignored `.env`

- [ ] **Step 1: Activate local configuration**

```dotenv
AGENT_PROVIDER=openai-compatible
AGENT_BASE_URL=https://api.deepseek.com
AGENT_MODEL=deepseek-v4-flash
AGENT_API_KEY=${DEEPSEEK_API_KEY}
AGENT_RESPONSE_FORMAT=json-object
```

- [ ] **Step 2: Prove Compose resolves the secret without printing it**

Parse `docker compose config --format json` and print only provider, URL, model, format, and whether the key is set. Expected: DeepSeek values match and the key reports `<set>`.

- [ ] **Step 3: Rebuild and recreate grading services**

```bash
docker compose up -d --build --force-recreate --wait api worker beat
```

Expected: all services run and API is healthy.

- [ ] **Step 4: Run a real provider smoke evaluation**

Inside the API container, instantiate the configured provider, submit a minimal Chinese data-structure assignment and answer, and pass the result through `normalize_evaluation`. Print only provider identity, model, score, and validation state; never print the credential or raw response.

Expected:

```text
provider=openai-compatible
model=deepseek-v4-flash
validation=passed
```

- [ ] **Step 5: Run full verification**

```bash
make verify
```

Expected: backend unit, backend acceptance, frontend unit, frontend build, Compose config, API readiness, Web readiness, and E2E all pass.

- [ ] **Step 6: Final safety checks**

```bash
git diff --check
git status --short
docker compose ps
```

Expected: no ignored `.env` or secret appears in Git and services remain healthy. Commit tracked corrections locally; do not push.
