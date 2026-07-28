# 验收测试结果

本页只记录已经实际执行的结果。时间均为 Asia/Shanghai（UTC+08:00）。2026-07-28 早间的 clean-room 记录保留当时“没有 Docker CLI”的真实状态；同日 19:00 后 Docker 已可用，新增的账号管理补验结果单独列在下方。真实 Mattermost 10.5 仍未在本轮启动，因此没有被写成“通过”。

## 账号管理增量验收

账号管理工作树于 2026-07-28 19:49:31–19:52:45 +08:00 从干净提交 `15b3dbfbecbea58e502e9d5f2c6baeb987c16bad` 执行 `scripts/verify_acceptance.py --local-only`，`run_id=20260728T194931517254+0800-b2a23657eb`；manifest 的实现 SHA 与该提交完全一致。结果为 **5 个已选门禁全部通过 / 3 个 Docker 门禁按 local-only 规则跳过**；manifest 没有 `cleanup_error` 或 `internal_error`。该轮使用私有 Unix socket、随机端口和自有 PostgreSQL 数据目录，结束时确认清理自有 Playwright、Web、API 与 PostgreSQL 进程。

| Check | Status | Return code | Duration | Fresh result |
| --- | --- | ---: | ---: | --- |
| `backend_unit` | **PASS** | 0 | 154.902s | 1,639 tests + 34 subtests；1 条既有 Starlette warning |
| `backend_acceptance` | **PASS** | 0 | 2.264s | 12 tests |
| `frontend_unit` | **PASS** | 0 | 7.668s | 24 files / 400 tests |
| `frontend_build` | **PASS** | 0 | 2.622s | TypeScript + Vite production build |
| `e2e` | **PASS** | 0 | 22.565s | 13 runner tests + 6 Playwright flows；Playwright 6/6 in 16.3s |

同一账号管理功能还在 Docker 29.6.2 / Compose v5.3.1 中以 `newproject` 项目完成 0001→0008 迁移与五用户 seed；API 和 Nginx Web 容器通过健康检查，worker 与 beat 容器保持运行（未为二者声明健康检查）。随后执行账号管理专用 Playwright 流程，结果为 **2/2 passed in 6.7s**：管理员通过 UI 创建教师、教师通过 UI 创建学生、新学生登录并看到种子作业、学生访问账号 API 得到 403；学生浏览器访问教师/管理员账号管理页均显示权限拒绝、未加载账号管理台，并可返回学生工作台；管理员访问作业列表/详情 API 得到 403，且前端教师路由拒绝访问。用户名使用每轮唯一后缀，不依赖残留账号。

验收器强制校验 Playwright manifest 为 4 个 spec / 6 个测试：`account-management.spec.ts`（2）、`full-flow.spec.ts`（2）、`mattermost-adapter.spec.ts`（1）、`seeded-reports.spec.ts`（1）。它会同时比对磁盘发现的 spec、实际通过行的逐文件数量和最终 passed 总数；漏登记、漏执行或数量不符都会使 `e2e` 门禁失败。账号创建会立即增加有效学生总数，因此 Mattermost 汇总测试将消息中的 total/submitted/missing 与同轮 REST summary 对照，不依赖固定人数。

## 历史 clean-room 统一验收清单

最终 clean-room 验收基线为 `b238a63bcb0c16f5b998650e71a517d96c23c350`。默认严格命令 `make verify` 于 2026-07-28 05:46:39–05:49:08 +08:00 实际运行，`run_id=20260728T054639609908+0800-e06781fae0`。结果为 **5 passed / 3 blocked**：所有与 Docker 无关的门禁通过；当前机器没有 `docker` 可执行文件，因此 3 个容器门禁没有被写成通过。本轮 manifest **未出现** `cleanup_error` 或 `internal_error` 字段。

主工作树的历史 baseline manifest、logs 和 Playwright report 是本机生成且被 Git 忽略的 raw artifacts，不能作为 fresh clone 中的 tracked 证据。本次 clean-room 只将不含命令、密钥、绝对临时路径和 raw logs 的 [脱敏结构化摘要](evidence/clean-room-acceptance.json) 提交到 Git；原 raw worktree/artifacts 在取证后定点删除。

| Check | Status | Return code | Duration | Stable evidence |
| --- | --- | ---: | ---: | --- |
| `backend_unit` | **PASS**：1,509 tests + 34 subtests，排除独立 acceptance 目录；1 条既有 Starlette warning | 0 | 119.275s | [clean-room summary](evidence/clean-room-acceptance.json) |
| `backend_acceptance` | **PASS**：12 tests | 0 | 1.776s | [clean-room summary](evidence/clean-room-acceptance.json)；`backend/tests/acceptance/` |
| `frontend_unit` | **PASS**：20 files / 337 tests | 0 | 6.148s | [clean-room summary](evidence/clean-room-acceptance.json) |
| `frontend_build` | **PASS**：TypeScript + Vite production build | 0 | 2.707s | [clean-room summary](evidence/clean-room-acceptance.json) |
| `compose_config` | **BLOCKED**：`docker` executable unavailable | 127 | 0.002s | [clean-room summary](evidence/clean-room-acceptance.json) |
| `api_ready` | **BLOCKED**：依赖 `compose_config` 通过后才在本项目容器内探测 | — | 0s | [clean-room summary](evidence/clean-room-acceptance.json) |
| `web_ready` | **BLOCKED**：依赖 `compose_config` 通过后才在本项目容器内探测 | — | 0s | [clean-room summary](evidence/clean-room-acceptance.json) |
| `e2e` | **PASS**：13 runner tests + 4 Playwright flows | 0 | 16.044s | [clean-room summary](evidence/clean-room-acceptance.json)；`e2e/tests/` |

同一实现路径还在下方 clean-room 中独立执行了 `make verify-local`，返回 0：5 个非 Docker 门禁全部为 `passed`，`compose_config`、`api_ready`、`web_ready` 明确为 `skipped`，不是伪造通过。统一验收器使用私有 Unix socket，并在 `createdb` 前核对随机 nonce 与 `data_directory`；关闭时按记录的 PID/PGID 停止自有 PostgreSQL，并在同一个清理宽限期内有限重试瞬时目录变化。删除仍失败时整轮必为失败，清单记录 `cleanup_error`，终端打印并保留精确目录，不会读取、覆盖或停止既有 5433 服务。

## 六类必测场景

六行均来自同一次隔离运行：runner 创建私有 socket 和临时 PostgreSQL data directory，执行迁移后运行 `backend/tests/acceptance`，最后按已记录 PID/PGID 停止 API、Vite 和 PostgreSQL，并删除自有临时目录。以下是命令模板；请把占位符换成该轮私有 socket 和实际端口：

```bash
TEST_DATABASE_URL='postgresql+asyncpg://postgres@/grader_acceptance_test?host=<url-encoded-private-socket>&port=<isolated-port>' \
PYTHONPATH=backend .venv/bin/pytest backend/tests/acceptance -q
```

本次总结果：`12 passed in 1.63s`。

| 必测场景 | Command / 覆盖用例 | Expected | Actual | Evidence | Timestamp |
| --- | --- | --- | --- | --- | --- |
| 正常完整答案 | 上述全套命令；`-k normal-complete-answer` | 输出字段完整，94/A，complete/correct，待教师审核 | **PASS**：94/A；`validation_status=valid`，`review_status=proposed`，1 次 Provider 调用 | `backend/tests/acceptance/test_required_scenarios.py`；`backend/tests/acceptance/cases.py` | 2026-07-28 03:42:17 +08:00 |
| 部分正确答案 | 上述全套命令；`-k partially-correct-answer` | 识别遗漏复杂度/边权限制，72/C | **PASS**：72/C；partial/mostly_correct，报告含 limitation 且待审核 | `backend/tests/acceptance/test_required_scenarios.py`；`backend/fixtures/agent_outputs/partial.json` | 2026-07-28 03:42:17 +08:00 |
| 明显错误答案 | 上述全套命令；`-k obviously-incorrect-answer` | 识别错误方向，35/D | **PASS**：35/D；incomplete/incorrect，报告含问题与限制 | `backend/tests/acceptance/test_required_scenarios.py`；`backend/fixtures/agent_outputs/incorrect.json` | 2026-07-28 03:42:17 +08:00 |
| 表述不完整/模糊 | 上述全套命令；`-k incomplete-ambiguous-answer` | 不武断判定；低置信度并要求人工审核 | **PASS**：45/D；incomplete/unable_to_determine，confidence ≤ 0.5，待教师审核 | `backend/tests/acceptance/test_required_scenarios.py`；`backend/fixtures/agent_outputs/ambiguous.json` | 2026-07-28 03:42:17 +08:00 |
| Agent 非法或不稳定输出 | 上述全套命令；`-k 'unstable_output or always_invalid_output'` | 非 JSON/语义错误可带错误类别修复；耗尽后安全失败，不伪造报告或泄露原始私密输出 | **PASS**：序列 `invalid_json → semantic → valid` 在第 3 次得到 `repaired` 报告；持续非法输出在 3 次后 `validation_exhausted`，job/audit 安全失败 | `backend/tests/acceptance/test_required_scenarios.py` | 2026-07-28 03:42:17 +08:00 |
| 当前能力边界 | 上述全套命令；`backend/tests/acceptance/test_capability_boundaries.py` | 不执行代码；缺 rubric 降低置信度；超长答案拒绝且保留旧版；Provider 故障保留提交；Prompt Injection 不能改 schema/fixture | **PASS**：6 个边界用例全部通过；代码报告声明“未执行代码”，50,001 字符被拒绝，网络错误只保存安全类别，注入文本保持为 untrusted student data | `backend/tests/acceptance/test_capability_boundaries.py` | 2026-07-28 03:42:17 +08:00 |

## 其他已执行验证

| 检查 | 实际命令 | 实际结果 | 稳定证据 | Timestamp |
| --- | --- | --- | --- | --- |
| 本地 E2E runner 自测 | `cd e2e && npm run test:runner` | **PASS**：13 tests，覆盖依赖失败、随机端口冲突、超时/中断、PID/PGDATA 所有权和清理失败保留策略 | `e2e/tests_runner/test_run_local_e2e.py`；[clean-room summary](evidence/clean-room-acceptance.json) | 2026-07-28 05:49:08 +08:00 |
| Playwright 完整流程 | `cd e2e && npm test` | **PASS**：4 tests / 9.4s；发布→提交→评估→确认、修改→重评→历史、Mattermost list/submit/summary 与 REST 一致，且三份种子历史报告在后续提交后仍可达 | `e2e/tests/full-flow.spec.ts`；`e2e/tests/mattermost-adapter.spec.ts`；`e2e/tests/seeded-reports.spec.ts`；[clean-room summary](evidence/clean-room-acceptance.json) | 2026-07-28 05:49:08 +08:00 |
| 前端组件与契约 | `cd frontend && npm test` | **PASS**：20 files，337 tests / 5.21s | `frontend/src/**/*.test.tsx`；`frontend/src/**/*.test.ts`；[clean-room summary](evidence/clean-room-acceptance.json) | 2026-07-28 05:49:08 +08:00 |
| Mattermost 报告链接返修 | `PYTHONPATH=backend .venv/bin/pytest backend/tests/mattermost/test_report_card.py -q`；隔离 PG 上单跑 openreport action | **PASS**：22 + 1；卡片和回调均使用真实前端路由 `/teacher/reports/{id}` | `backend/tests/mattermost/test_report_card.py`；`backend/tests/mattermost/test_actions_service.py` | 2026-07-28 03:41 +08:00 |
| 后端非验收基线 | 统一验收器的 `backend_unit` | **PASS**：1,509 tests + 34 subtests；排除 `backend/tests/acceptance`；1 条既有 Starlette warning | [clean-room summary](evidence/clean-room-acceptance.json) | 2026-07-28 05:49:08 +08:00 |
| 验收器生命周期与证据契约 | `PYTHONPATH=backend .venv/bin/pytest scripts/tests/test_verify_acceptance.py -q` | **PASS**：46 tests + 34 subtests，覆盖 PG 强归属、共享 deadline、并发锁、进程组/脱离子进程、1 MiB spool、脱敏、中断、run-id 证据与清理失败 | `scripts/tests/test_verify_acceptance.py` | 2026-07-28 05:33 +08:00 |

## Clean-room 启动与最终复验

从 `b238a63bcb0c16f5b998650e71a517d96c23c350` 创建 detached disposable worktree，开始时只有 Git tracked files，然后复制 `.env.example` 为被忽略的 `.env`。在该目录中新建独立 `.venv`，执行 `make install`、`make install-frontend`、E2E `npm ci` 和 Playwright Chromium 安装；没有复用主工作树的 `.venv` 或 `node_modules`。

| 项目 | 实际记录 |
| --- | --- |
| 平台 | macOS 26.5.2 (25F84), arm64；Python 3.12.13；Node 25.9.0；npm 11.12.1；PostgreSQL 18.4 |
| Docker | `docker --version` 返回 command not found；没有 Docker/Compose 版本可记录 |
| 时间窗口 | 2026-07-28 05:46:21–05:51:45 +08:00（fresh install 后 strict + local-only） |
| strict `make verify` | `run_id=20260728T054639609908+0800-e06781fae0`，05:46:39–05:49:08；5 passed / 3 blocked；manifest 未出现 cleanup/internal error 字段 |
| `make verify-local` | `run_id=20260728T054910890396+0800-2e7ed49426`，05:49:10–05:51:33；5 selected gates passed / 3 Docker gates skipped；返回 0；manifest 未出现 cleanup/internal error 字段 |
| 清理 | strict verifier PostgreSQL PID 21587、local-only PID 23579 均已确认退出；两轮 E2E 日志均记录自有 Playwright/Web/API/PostgreSQL 停止；安全摘要见 [clean-room evidence](evidence/clean-room-acceptance.json) |

这次 clean-room 不能把 Docker 启动记为通过；它证明的是：从纯 tracked files 可新建依赖环境，并在本机以私有 PostgreSQL/API/Vite/Playwright 栈完整通过非 Docker 验收门禁。ignored raw manifests/logs 没有复制回仓库；Git 中只保留脱敏结构化摘要。

## 演示技术预演

本轮是自动化技术预演，不是真人 20 分钟现场演示：

- 正式 tracked `e2e/tests/seeded-reports.spec.ts` 在 Mattermost 新增 student1 v2 之后执行，通过固定 seed submission ID 只读查询历史 Agent 报告，再以教师 UI 打开真实 report route；它不依赖文件顺序、不 reset 数据、不直写报告。
- 该 spec 断言 `94/A`、`72/C`、`35/D`的完整性、正确性、不同诊断、校验/审核状态，并确认可见页面不含演示 token、setup key、密码或 access token。strict clean-room 中该 spec **1.9s passed**，低于 30 秒 fallback 目标；全部 Playwright **4/4 passed in 9.4s**；local-only 轮分别为 1.8s 与 9.1s。
- 其他 E2E 继续覆盖教师确认、修改为 88/B 并保留 v1/v2、重新评估产生 v3；Mattermost adapter 覆盖 `/hw list`、submit、summary 与 REST 一致性。
- 通过测试没有生成失败截图；后续证据与 tracked-file 扫描未发现真实 token、私钥或非示例密码。现场共屏仍须按 [演示脚本](demo-script.md) 避免打开 `.env`。
- **待办**：未安排管理员，未完成真人 20 分钟计时，也未录制替代视频；这些属于外部交付步骤。

## 当前未验证项

| 检查 | 当前状态 | 原因与下一步 |
| --- | --- | --- |
| `docker compose config --quiet` | **BLOCKED（已记录）** | `make verify` 实际调用后得到 rc=127；当前机器无 Docker CLI。Compose YAML 与 pytest contract 已静态检查；需在有 Docker 的 clean-room 重跑。 |
| `make demo` | **BLOCKED（命令已执行）** | clean-room 实际运行后 Make rc=2、内部 `docker` rc=127，在 `db-up` 处停止；`db-up → migrate → seed → api-up → web → health` 容器服务链未启动验证。 |
| `make demo-full` / `make mattermost-configure` | **BLOCKED（`demo-full` 命令已执行）** | `make demo-full` 实际运行后在 `mattermost-up` 处因缺少 Docker 停止，未到达 `mattermost-configure`；配置脚本测试与 E2E adapter 已通过，但真实 Mattermost 10.5 服务未启动验证。 |
| Nginx production image / same-origin proxy | **未实跑容器** | 前端生产 build 和 Nginx 配置由代码/测试静态覆盖；需在 Docker 环境执行 build、`/healthz` 和 `/api/` smoke test。 |
| OpenAI-compatible 真实 Provider | **未验证外部供应商** | 验收使用确定性 mock；真实可用性、费用、限流与延迟取决于用户配置。 |

## Docker 环境补验命令

在有 Docker 的全新 clone 中执行，并把生成时间、Docker/Compose 版本与 commit SHA 追加到本页：

```bash
cp .env.example .env
make install
make demo
make health
make verify
make demo-full
curl --fail http://localhost:8080/healthz
curl --fail http://localhost:8065/api/v4/system/ping
```

运行 `make demo-full` 前，必须按 [mattermost-setup.md](mattermost-setup.md) 设置管理员密码、26 位 Slash Token 和 demo setup key；完整 Bot 私信还需 Bot Token/User ID 与 Action Secret。
