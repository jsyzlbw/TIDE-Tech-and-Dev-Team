# Mattermost 接入与本地演示配置

## 一次性准备

先复制环境模板并安装后端依赖：

```bash
cp .env.example .env
make install
```

在 `.env` 中设置以下本机演示值。不要把真实 Token 提交到 Git：

```dotenv
MATTERMOST_ADMIN_USERNAME=admin
MATTERMOST_ADMIN_EMAIL=admin@example.com
MATTERMOST_ADMIN_PASSWORD=replace-with-local-admin-password

# 必须是 26 位小写字母/数字；脚本会让 Mattermost 命令与 API 使用同一值。
MATTERMOST_COMMAND_TOKEN=0123456789abcdefghijklmnop
MATTERMOST_DEMO_SETUP_KEY=replace-with-random-demo-binding-key

# 创建 Bot 后补齐；只注入 worker。
MATTERMOST_URL=http://mattermost:8065
MATTERMOST_BOT_TOKEN=replace-with-bot-token
MATTERMOST_BOT_USER_ID=replace-with-26-char-bot-user-id
MATTERMOST_ACTION_SECRET=replace-with-random-action-secret
MATTERMOST_ACTION_URL=http://api:8000/api/v1/integrations/mattermost/actions
WEB_CONSOLE_URL=http://localhost:8080
```

`MATTERMOST_DB_PASSWORD` 还必须保留为至少 16 位、只含 `[A-Za-z0-9._~-]` 的原始值；同一原始值同时用于 PostgreSQL 密码和连接串，不能只对一处做 URL 编码。

## 完整启动路径

```bash
make demo-full
```

实际依赖顺序为：

1. `mattermost-up`：校验数据库密码，启动 `mattermost-db` 与 Mattermost 10.5 并等待健康；
2. `demo`：启动开发/测试 PostgreSQL 与 Redis，迁移，写入种子，启动 API/worker/beat/Web 并检查 API readiness；
3. `mattermost-configure`：从宿主机登录 Mattermost，同时通过 `http://localhost:8000` 调用开发绑定路由；Slash Command 的容器内 Request URL 使用 `http://api:8000`。

配置脚本幂等创建或核对：

- Team：`data-structures`（Data Structures）；
- Channel：`course-home`（Course Home）；
- 用户：`teacher`、`student1`、`student2`、`student3`；
- Team/Channel membership；
- POST Slash Command：trigger `hw`；
- 四个 Mattermost User ID 到本地账号的绑定。

脚本只接受与 `MATTERMOST_COMMAND_TOKEN` 完全一致的现有 `/hw` 命令，不会在不知情时替换一个不同 Token。需要单独重跑配置时：

```bash
make mattermost-configure
```

本机入口：<http://localhost:8065>。四个 Mattermost 演示账号与 Web 使用相同密码：教师 `Teacher123!`，三名学生 `Student123!`。

## Bot Token 与报告私信

`make mattermost-configure` 不创建高权限 Bot。使用管理员在 Mattermost 创建专用 Bot 账号，记录其 26 位 User ID 与 Token，把它们写入 `MATTERMOST_BOT_USER_ID` 和 `MATTERMOST_BOT_TOKEN`，然后重建 worker：

```bash
make api-up
```

worker 使用 `Authorization: Bearer <Bot Token>` 调用 Mattermost v4 API：

1. `POST /api/v4/channels/direct`，正文为 `[bot_user_id, teacher_user_id]`；
2. `POST /api/v4/posts`，向该双人私聊发送报告卡片。

Bot Token 不进入 API、beat、前端、Slash 请求或按钮 context。客户端禁用重定向，限制连接/读取超时和 64 KiB 响应；401/403 作为永久认证失败，408/429/5xx 才进入受限重试。

报告私信固定标记“AI 基础评估 · 待教师审核”，展示分数/等级、完整性、主要问题、建议、限制及三个按钮；不包含学生原始答案、rubric、Provider 原始输出或密钥。

## Slash Command 请求契约

Request URL：

```text
http://api:8000/api/v1/integrations/mattermost/commands
```

Request Method 必须是 `POST`，Content-Type 必须是 UTF-8 `application/x-www-form-urlencoded`。适配器只接受一次且仅一次以下字段，未知字段、重复字段、非法 UTF-8、无效 percent escape 或超过 64 KiB 的正文会被拒绝：

| 字段 | 用途 |
| --- | --- |
| `token` | Mattermost 为该 Slash Command 保存的 26 位 Token；只用于传输鉴权，不进入请求哈希。 |
| `team_id` / `team_domain` | Team 身份与显示信息。 |
| `channel_id` / `channel_name` | 当前频道；发布作业时 `channel_id` 记录为作业来源频道。 |
| `user_id` / `user_name` | 发起者；授权只信任不可变 `user_id`，username 仅显示。 |
| `command` | 必须精确为 `/hw`。 |
| `text` | Slash 后的参数文本，最大 60 KiB UTF-8。 |
| `trigger_id` | 本次交互的唯一触发标识，参与幂等哈希。 |
| `response_url` | Mattermost 回调 URL；当前基础命令只做校验，不依赖它完成业务。 |

Token 在打开数据库 session 之前用 `hmac.compare_digest` 做常量时间比较。认证失败统一返回 401 的临时消息，不回显期望 Token、实际 Token 或解析细节。

## 本地身份绑定

development/test 且设置 `MATTERMOST_DEMO_SETUP_KEY` 时，API 才注册：

```text
POST /api/v1/integrations/mattermost/demo-bindings
X-Demo-Setup-Key: <key>
```

请求示例：

```bash
curl --fail-with-body -X POST \
  http://127.0.0.1:8000/api/v1/integrations/mattermost/demo-bindings \
  -H 'Content-Type: application/json' \
  -H 'X-Demo-Setup-Key: replace-with-random-demo-binding-key' \
  -d '{
    "local_username": "teacher",
    "mattermost_user_id": "replace-with-26-char-user-id",
    "mattermost_username": "teacher"
  }'
```

绑定的授权轴是 Mattermost User ID ↔ 本地 User ID。一侧已经绑定其他身份时返回 409；相同绑定重复执行返回 200 并只更新显示 username。每次命令/按钮仍重新检查本地账号 active 与 role。该路由在 staging/production 不注册，也不出现在生产 OpenAPI。

## 支持的命令

```text
/hw help
/hw publish --title "标题" --due "YYYY-MM-DD HH:MM" --question "题目" [--notes "说明"]
/hw list
/hw show HW-0001
/hw submit HW-0001 --text "答案"
/hw summary HW-0001
/hw evaluate HW-0001
```

- `publish`：仅教师；截止时间按 `Asia/Shanghai` 解析并转换为 UTC，直接创建 `published` 作业。
- `list`：教师可见全部首批 20 份，学生只见 `published/closed`。
- `show`：显示题目与可选说明，但不显示 rubric。
- `submit`：仅学生，提交 `text` 类型答案并追加版本，来源记录为 `mattermost`。
- `summary`：仅教师，只返回人数和队列/审核聚合，不泄露其他学生答案。
- `evaluate`：仅教师，原子写批量 job + outbox，立即返回 `queued/skipped`，不在 2.5 秒命令窗口内同步调用 Agent 或 Redis。

完整 URL-encoded curl 示例见 [api-examples.md](api-examples.md)。

## Interactive Action 回调

报告卡片按钮向以下地址发送严格 JSON：

```text
POST /api/v1/integrations/mattermost/actions
Content-Type: application/json
```

顶层字段必须恰好是 `user_id`、`post_id`、`channel_id`、`team_id`、`context`；私信 action 的 `team_id` 必须为空。v2 `context` 必须恰好包含：

```json
{
  "action": "confirm | reevaluate | openreport",
  "report_id": "UUID",
  "delivery_id": "UUID",
  "expected_user_id": "Mattermost user id",
  "expected_channel_id": "direct channel id",
  "signature": "64-char lowercase hex HMAC"
}
```

HMAC-SHA256 使用 `MATTERMOST_ACTION_SECRET`，并以长度前缀绑定 action、report、delivery、期望用户和期望频道。API 在获取数据库 session 前用常量时间比较验签，并再次常量时间核对实际 `user_id/channel_id`。随后数据库还要求：

- `delivery_id + report_id` 对应一条已成功投递、未失败的 notification outbox；
- `post_id` 等于真实 Mattermost post ID；
- job 的 `requested_by` 等于当前绑定教师；
- 目标报告仍是当前可审核版本。

`confirm` 与 `reevaluate` 把业务变化和幂等事件放在同一事务；`openreport` 返回真实前端路径 `/teacher/reports/{report_id}`。复制按钮到其他人、频道或帖子无法通过验证。重复 callback 返回第一次保存的响应，不新增审核动作。

## 生产化要求

- Mattermost、API、Action URL 和 Web Console 全部使用 HTTPS；
- 定期轮换 Slash Token、Bot Token、Action Secret、JWT Secret 与数据库密码；
- 限制 CORS、反向代理来源、防火墙和 Bot 权限；
- 禁用 development/test demo binding route；
- 监控 notification outbox 的 `failed_at/last_error_type` 与可能的 at-least-once 重复投递；
- 不在 URL、日志、截图、命令历史或 Mattermost 消息中展示 Token。
