# Mattermost AI 作业评审系统

面向数据结构课程的可运行验收项目：教师发布作业，学生提交文本、Markdown、代码或结构化答案，系统汇总最新版本，AI Agent 异步生成基础评估，教师再确认、修改或要求重新评估。Mattermost Slash Command 与报告私信复用同一套领域服务和审计链路。

AI 结果只作教师审核的起点，不是自动裁决。

## 功能闭环

1. 教师在 Web 或 Mattermost 发布作业，内容包括标题、题目、截止时间、评分要点与可选说明。
2. 学生在 Web 或 Mattermost 提交答案；每次提交追加新版本，历史版本保留。
3. 教师查看全班汇总，系统只评估每名学生的最新有效提交。
4. Celery worker 调用 mock 或 OpenAI-compatible Provider，校验结构化输出并生成报告。
5. 报告覆盖完整性、正确性、主要问题、修改建议、分数/等级、置信度与能力边界。
6. 教师确认、生成修订版本，或发起重新评估；原报告和审核动作保留为证据。
7. 已绑定 Mattermost 身份的教师可收到报告私信，并使用“确认评估 / 重新评估 / 打开报告”按钮。

默认种子作业 `HW-0001` 预置三类可重复演示的报告：完整正确 `94/A`、部分正确 `72/C`、明显错误 `35/D`。

## 架构概览

默认 Compose 运行 PostgreSQL、Redis、FastAPI、Celery worker/beat、React 静态站点与 Nginx；`mattermost` profile 再加入 Mattermost Team Edition 及其独立 PostgreSQL。评估请求先写数据库 job/outbox，再由 worker 异步执行，避免 HTTP 请求直接依赖 Redis 或 Provider。

完整组件图、时序、数据所有权、幂等与失败流见 [docs/architecture.md](docs/architecture.md)。

## 环境要求

五分钟 Compose 演示需要：

- Docker Desktop 或其他支持 `docker compose` 的 Docker Engine；
- GNU Make 与 Python 3.12；
- 至少 4 GB 可用内存，完整 Mattermost 演示建议 8 GB。

宿主机 E2E 不使用 Docker，除 GNU Make 与 Python 3.12 外还需要：

- PostgreSQL 16+ 的本机 server/CLI，且 `initdb`、`pg_ctl`、`createdb` 可执行；runner 会自行创建随机端口和临时 data directory，不使用已有业务数据库；
- `lsof`，用于确认随机端口及安全清理 runner 自己启动的进程；
- Node.js `22.22.x` 或 `24–25` 与 npm `11.x`；
- 前端 `node_modules`、E2E npm 依赖和 Playwright Chromium，按下方 fresh-clone 命令安装。

`jq` 只在复制 [API 示例](docs/api-examples.md) 时需要。macOS 和 Linux 均可按上述可执行文件核对环境；本项目不假设特定包管理器。

所有端口只绑定到 `127.0.0.1`。共享或公开部署前必须替换 `.env` 中的示例密钥，并配置 HTTPS。

## 五分钟启动

从仓库地址克隆后执行：

```bash
git clone <repository-url> mattermost-ai-grading
cd mattermost-ai-grading
cp .env.example .env
make install
make demo
```

`make demo` 依次启动数据库/Redis、执行迁移、写入幂等演示种子、启动 API/worker/beat、构建并等待 Web 健康。迁移前尚未启动业务进程；如果旧数据库仍有其他连接，迁移会安全拒绝，而不会终止未知进程。

启动后使用三个固定入口：

- Web 控制台：<http://localhost:8080>
- API 根路径：<http://localhost:8000/api/v1>（OpenAPI：<http://localhost:8000/docs>）
- Mattermost：<http://localhost:8065>（仅执行 `make demo-full` 后可用）

检查正在运行的 API，再执行当前代码级验证：

```bash
make health
make verify
```

`make health` 请求 `/health/ready`，要求 API 与数据库均就绪。`make verify` 是严格的统一验收入口：它运行后端非验收测试、独立六类验收矩阵、前端测试与生产构建、Compose 配置、容器内 API/Web 健康检查和自清理 E2E。本机 `artifacts/acceptance/manifest.json`、`runs/<run_id>/`、logs 和 Playwright report 均是 ignored 生成物；仓库只提交目录占位与 [clean-room 脱敏摘要](docs/evidence/clean-room-acceptance.json)。任何选中门禁被阻塞、超时、清理失败或执行失败都会返回非零；因此应在 `make demo` 成功后执行。

`make demo` 是 Docker 路径，不要求宿主机安装 PostgreSQL/Node.js。Compose 固定使用 PostgreSQL 16，以保证演示环境可复现；宿主机隔离验证支持 PostgreSQL 16+。若要从 fresh clone 在宿主机实跑隔离 E2E，必须先安装两套 npm 依赖和 Chromium：

```bash
cp .env.example .env
make install
make install-frontend
make e2e-install
make e2e
test -f artifacts/acceptance/playwright/index.html
```

这里 `make install-frontend` 提供 Vite 所需的 `frontend/node_modules`；`make e2e-install` 提供 E2E 依赖并安装 Playwright Chromium。E2E runner 还要求上一节列出的 PostgreSQL 16+ CLI/server 与 `lsof`，但不要求 Docker 正在运行。

本仓库当前环境已完成本地隔离 PostgreSQL + API + Vite 的 E2E 实跑；账号管理版本还在 Docker 29.6.2 / Compose v5.3.1 中完成迁移、五用户种子、API/Web 健康检查与专用 Playwright 流程。真实 Mattermost 10.5 容器交互仍未在本轮启动验证，详见 [docs/test-results.md](docs/test-results.md)。

停止服务并保留数据库卷：

```bash
make db-down
```

## 演示账号

| 角色 | 用户名 | 密码 | 种子报告 |
| --- | --- | --- | --- |
| 系统管理员 | `admin` | `Admin123!Secure` | 可创建教师或学生本地账号；不能访问课程业务数据 |
| 教师 | `teacher` | `Teacher123!` | 可查看和审核全部报告 |
| 学生甲 | `student1` | `Student123!` | `94 / A`，完整正确 |
| 学生乙 | `student2` | `Student123!` | `72 / C`，部分正确 |
| 学生丙 | `student3` | `Student123!` | `35 / D`，明显错误 |

这些凭据只用于本机 development/test 演示，不能用于共享或生产环境。`admin` 是本系统的本地管理员账号，与 Mattermost 服务器管理员不是同一个身份，也不会自动创建或绑定 Mattermost 账号。建议用五个独立浏览器 Profile 预先登录，以便在 20 分钟验收中快速切换角色。

## 账号管理

- 本地管理员登录后进入 <http://localhost:8080/admin/users>，可以创建教师或学生账号；管理员不能创建其他管理员，也不能访问作业、提交、评估或教师审核数据。
- 教师从工作台进入 <http://localhost:8080/teacher/users>，只能创建和查看学生账号；学生不能访问账号管理页面或 API。
- 账号列表与单账号创建共用 `GET /api/v1/users`、`POST /api/v1/users`。管理员列表可见本地管理员、教师和学生，教师列表只显示学生；完整 curl 示例见 [docs/api-examples.md](docs/api-examples.md)。
- 用户名必须是 3–64 位小写 ASCII 字母、数字、点、下划线或连字符，且首字符只能是字母或数字；系统会去掉首尾空白，但不会把大写字母自动转成小写。初始密码必须为 12–128 个字符，不能带首尾空白或控制/格式字符，也不能与用户名相同。同名用户名返回 HTTP `409`。
- 当前 v1 每次只创建一个 active 本地账号，不提供自助注册、编辑、删除、密码重置或停用/重新启用。新建 active 学生会立即进入这个单课程演示的全局学生池，因此所有既有作业的总人数和未交人数都会同步增加。
- 新建账号不会自动创建或绑定 Mattermost 用户。需要 Mattermost 交互时，必须通过受控的显式绑定流程关联 Mattermost 返回的不可变 User ID；本地 `admin` 也不等于 Mattermost 服务器管理员。

文档中的固定密码和占位变量只适用于本机演示。不要把真实生产密码、Bearer Token、Mattermost Token 或 Provider API Key 直接写进命令行、共享 shell history、Git、截图或录屏；真实环境应使用受控的 secret 注入方式。

## Mattermost 接入

在 `.env` 中设置 Mattermost 服务器管理员密码、26 位 Slash Command Token、开发绑定 Key，并按需补齐 Bot Token、Bot User ID、Action Secret 与三个 URL。然后执行：

```bash
make demo-full
```

该命令启动 Mattermost profile，再复用安全的 `make demo` 启动完整 Web 栈，最后幂等创建 `data-structures` Team、`course-home` Channel、教师与三名学生共四个 Mattermost 账号、`/hw` 命令和本地身份绑定。上表中的本地 `admin` 不属于这四个 Mattermost 身份。Bot 账号/Token 需要在 Mattermost 中创建后写入 `.env`；配置脚本不会伪造生产 Bot 权限。

支持的命令：

```text
/hw help
/hw publish --title "标题" --due "YYYY-MM-DD HH:MM" --question "题目" [--notes "说明"]
/hw list
/hw show HW-0001
/hw submit HW-0001 --text "答案"
/hw summary HW-0001
/hw evaluate HW-0001
```

接入字段、Token 验证、身份绑定、Bot 私信和按钮签名见 [docs/mattermost-setup.md](docs/mattermost-setup.md)。

## 测试与验收

fresh clone 先执行“环境要求”中的 `make install`、`make install-frontend` 和 `make e2e-install`。依赖就绪后再运行：

```bash
make verify              # 严格运行全部 8 个门禁；Compose 服务须已启动
make verify-local        # 无 Docker 时运行 5 个本机门禁，3 个容器门禁明确记为 skipped
make verify-backend      # 独立运行后端测试 + Ruff lint/format
make verify-frontend     # 401 个组件/契约测试 + ESLint + 生产构建
make e2e                 # 13 个 runner 测试 + 6 个 Playwright 流程
```

统一验收器在仓库级非阻塞锁内运行，避免两个实例交叉覆盖证据。`--total-timeout` 覆盖临时数据库初始化和全部门禁，结束后只有一段显式、有界的清理宽限期；失败后仍继续收集互不依赖的证据。后端测试只连接当前运行私有的 Unix socket，并在建库前核对 PostgreSQL `data_directory` 与随机 nonce，不读取或停止默认 `localhost:5433` 等外部实例；E2E 使用自己的自清理本地栈。fresh clone 缺少锁定依赖时会给出对应安装命令，不会自动联网安装。命令原始输出写入立即 unlink、权限 `0600` 的磁盘 spool；原始临时写入不承诺字节上限，但内存读回、返回内容和执行时间均有界，超出阈值只返回大小元数据。持久化日志经过敏感字段脱敏并限制大小；每轮清单只保存状态、耗时和该 `run_id` 下的证据路径。

六类必测场景已自动化：完整正确、部分正确、明显错误、模糊/不完整、Agent 非法或不稳定输出、当前能力边界。可复核命令、实际结果、时间戳与证据路径见 [docs/test-results.md](docs/test-results.md)。15–20 分钟现场流程见 [docs/demo-script.md](docs/demo-script.md)。

## AI Provider 切换

`.env.example` 对应的真实配置名是 `AGENT_*`：

```dotenv
AGENT_PROVIDER=mock
AGENT_MOCK_FIXTURE=partial
```

`AGENT_PROVIDER=mock` 不联网，输出由显式 fixture 决定，适合可重复验收。它不会根据学生答案关键词自动挑选高分或低分结果。

接入 OpenAI-compatible Chat Completions 服务时：

```dotenv
AGENT_PROVIDER=openai-compatible
AGENT_BASE_URL=https://provider.example/v1
AGENT_API_KEY=replace-with-provider-key
AGENT_MODEL=replace-with-model-id
AGENT_RESPONSE_FORMAT=json-schema
```

DeepSeek 使用 OpenAI-compatible Chat Completions，但其 JSON Output 模式需要单独配置：

```dotenv
AGENT_PROVIDER=openai-compatible
AGENT_BASE_URL=https://api.deepseek.com
AGENT_MODEL=deepseek-v4-flash
AGENT_API_KEY=${DEEPSEEK_API_KEY}
AGENT_RESPONSE_FORMAT=json-object
```

先在本机 Shell 中设置 `DEEPSEEK_API_KEY`；项目 `.env` 只引用该变量，不保存密钥明文。API Key 在选择 `openai-compatible` 时必填；Base URL 不应包含 `/chat/completions`。`json-schema` 是默认模式，`json-object` 用于 DeepSeek 等只支持 JSON Object 的兼容服务。非 loopback HTTP 默认拒绝，可通过 `AGENT_ALLOW_INSECURE_HTTP=true` 仅在受控开发环境中显式放开。修改 `.env` 后重建 API、worker 与 beat：

```bash
make api-up
```

真实 Provider 的可用性、限流、成本、延迟与模型准确性均属于外部依赖；现场验收优先使用 mock/种子结果。

## 数据重置

`make seed` 是幂等的：演示数据完整时重复执行不会复制用户、作业、提交或报告；发现固定 ID/账号被非演示数据占用时会拒绝覆盖。

若需完全回到干净的本机演示状态，可删除当前 Compose 项目的数据卷后重建：

```bash
docker compose --profile mattermost down -v
make demo
```

`down -v` 会永久删除本项目的 PostgreSQL、Mattermost 数据与配置卷，仅可用于确认无保留价值的本机演示数据。普通停机请使用 `make db-down`，它保留卷。

## 当前能力边界

- AI 输出可能错误，必须由教师确认；
- 代码答案只作为文本评审，不会在沙箱中执行；
- 模糊题目、不完整答案或缺失 rubric 会降低置信度；
- 当前角色模型适合课程演示，不是院校级权限平台；
- 不宣称支持查重、文件 OCR 或隐藏测试执行；
- 老师提供的 CSC3100 模拟数据集包含名册、ZIP 提交、异常格式、迟交/缺交、公开/隐藏用例和 AI 写作风险标记，仅作为外部 QA 与后续扩展参考。当前 v1 不摄入这些 CSV/ZIP/manifest，不隔离异常压缩包，也不会从数据集元数据自动建立迟交、格式无效或缺交语义；不过现有作业汇总会把每名没有最新提交的 active 学生标为未交。系统不执行公开或隐藏代码测试，也不检测 AI 写作；风险标记只能作为教师复核提示，不能自动扣分。提交渠道的现有范围是：Web 支持文本、Markdown 和代码文本，Mattermost `/hw submit` 创建文本提交，REST API 还支持结构化 JSON；代码均只作文本评审。
- Mattermost 演示认证经过 Token/HMAC 和本地绑定，但生产仍需 HTTPS、密钥轮换与受限来源；
- 通知采用可恢复的 at-least-once outbox，极端崩溃窗口可能产生重复私信，不宣称 exactly-once。

完整边界与降级策略见 [docs/limitations.md](docs/limitations.md)。

## 目录结构

```text
backend/                 FastAPI、SQLAlchemy、Celery、迁移与 pytest
frontend/                React、TypeScript、Vite 与 Nginx 静态站点
e2e/                     Playwright 流程与隔离本地栈 runner
scripts/                 Mattermost 幂等配置及运维脚本
docs/                    架构、接入、演示、测试与边界文档
artifacts/acceptance/     验收 raw manifest/log/report 本机生成且 ignored（Git 只提交占位）
docs/evidence/            可提交的 clean-room 脱敏结构化摘要
docker-compose.yml        默认运行时与可选 Mattermost profile
Makefile                  安装、启动、验证和演示入口
.env.example              无真实密钥的本机配置模板
```
