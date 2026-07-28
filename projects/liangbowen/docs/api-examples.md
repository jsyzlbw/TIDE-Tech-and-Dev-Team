# REST 与 Mattermost API 示例

以下命令对应当前 FastAPI schema。先完成 `make demo`，安装 `jq`，并在仓库根目录执行。所有 ID 都从响应中提取，不把展示用 `HW-0001` 当作 REST UUID。

## 公共变量与登录

```bash
API=http://127.0.0.1:8000/api/v1

ADMIN_TOKEN=$(
  curl --fail-with-body --silent --show-error \
    -X POST "$API/auth/login" \
    -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"Admin123!Secure"}' |
  jq -er '.access_token'
)

TEACHER_TOKEN=$(
  curl --fail-with-body --silent --show-error \
    -X POST "$API/auth/login" \
    -H 'Content-Type: application/json' \
    -d '{"username":"teacher","password":"Teacher123!"}' |
  jq -er '.access_token'
)

STUDENT_TOKEN=$(
  curl --fail-with-body --silent --show-error \
    -X POST "$API/auth/login" \
    -H 'Content-Type: application/json' \
    -d '{"username":"student1","password":"Student123!"}' |
  jq -er '.access_token'
)

curl --fail-with-body --silent --show-error \
  "$API/auth/me" \
  -H "Authorization: Bearer $TEACHER_TOKEN" | jq
```

登录响应是 `{"access_token":"…","token_type":"bearer"}`。上述固定账号密码只适用于本机 development/test 演示。不要把真实生产密码、Bearer Token、Mattermost Token 或 Provider API Key 直接写入命令行、URL、Git、截图或共享 shell history；真实环境应使用受控的 secret 注入方式。

## 本地账号管理

Web 入口是管理员的 <http://localhost:8080/admin/users> 和教师的 <http://localhost:8080/teacher/users>。管理员可以创建教师或学生；教师只能创建学生；学生以及未登录请求都会被拒绝。管理员只管理本地账号，不能读取作业、提交、评估或审核数据。

管理员查看全部本地账号（教师使用同一请求时只会看到学生）：

```bash
curl --fail-with-body --silent --show-error \
  "$API/users?limit=50&offset=0" \
  -H "Authorization: Bearer $ADMIN_TOKEN" |
jq '{total, limit, offset, items}'
```

管理员创建教师：

```bash
curl --fail-with-body --silent --show-error \
  -X POST "$API/users" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{
    "username": "teacher2",
    "display_name": "教师乙",
    "role": "teacher",
    "password": "Course2026!Secure"
  }' |
jq '{id, username, display_name, role, is_active, created_at, created_by}'
```

管理员创建学生；教师也可以把 Authorization Token 换为 `$TEACHER_TOKEN` 执行同一类学生创建请求：

```bash
curl --fail-with-body --silent --show-error \
  -X POST "$API/users" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{
    "username": "student4",
    "display_name": "张三",
    "role": "student",
    "password": "Course2026!Secure"
  }' |
jq '{id, username, display_name, role, is_active, created_at, created_by}'
```

`POST /api/v1/users` 成功返回 `201`，响应不包含密码或密码哈希。用户名必须是 3–64 位小写 ASCII 字母、数字、点、下划线或连字符，首字符只能是字母或数字；首尾空白会被去掉，大写不会自动转小写。显示名去除首尾空白后须为 1–128 个可见字符，不能包含控制/格式字符。密码须为 12–128 个字符，不能有首尾空白或控制/格式字符，也不能与用户名相同。请求体上限为 4 KiB。

重复用户名返回 `409` 与 `{"detail":"username already exists"}`；格式错误返回 `422`；越权创建返回 `403`。任何调用方都不能通过该 API 创建 `admin`。当前没有自助注册、批量创建、编辑、删除、密码重置或停用/重新启用 API。新建 active 学生会立刻加入所有既有作业共享的全局学生池，使相应汇总中的 `total_students` 和 `missing_students` 增加。

该 API 只创建本地账号，不会创建或绑定 Mattermost 用户。Mattermost 使用前仍须按本文后面的 demo binding 或 [Mattermost 接入说明](mattermost-setup.md)完成显式绑定；本地管理员与 Mattermost 服务器管理员是不同身份。

## 创建并发布作业

```bash
ASSIGNMENT=$(
  curl --fail-with-body --silent --show-error \
    -X POST "$API/assignments" \
    -H "Authorization: Bearer $TEACHER_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{
      "title": "验收 API：Dijkstra",
      "question": "说明 Dijkstra 算法的核心思想、复杂度与适用条件。",
      "notes": "请给出关键松弛步骤。",
      "rubric": {
        "required_points": ["非负权限制", "松弛过程", "复杂度"],
        "grading_notes": "以概念正确和边界完整为准。"
      },
      "due_at": "2098-12-31T23:59:00+08:00"
    }'
)
ASSIGNMENT_ID=$(printf '%s' "$ASSIGNMENT" | jq -er '.id')
ASSIGNMENT_CODE=$(printf '%s' "$ASSIGNMENT" | jq -er '.code')
printf 'created %s %s\n' "$ASSIGNMENT_CODE" "$ASSIGNMENT_ID"

PUBLISHED=$(
  curl --fail-with-body --silent --show-error \
    -X POST "$API/assignments/$ASSIGNMENT_ID/publish" \
    -H "Authorization: Bearer $TEACHER_TOKEN"
)
printf '%s' "$PUBLISHED" | jq '{id, code, status, due_at}'
```

create 返回 `201` 和 draft `AssignmentRead`；publish 返回 `200`，`status` 应为 `published`。`due_at` 必须带时区，服务端统一转 UTC。

## 学生提交与版本

```bash
SUBMISSION=$(
  curl --fail-with-body --silent --show-error \
    -X POST "$API/assignments/$ASSIGNMENT_ID/submissions" \
    -H "Authorization: Bearer $STUDENT_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{
      "content_type": "text",
      "content_text": "Dijkstra 反复取当前距离最小的顶点并松弛出边，只适用于非负权图；使用邻接表和二叉堆时为 O((V+E)logV)。",
      "content_json": null
    }'
)
SUBMISSION_ID=$(printf '%s' "$SUBMISSION" | jq -er '.id')
printf '%s' "$SUBMISSION" | jq '{id, assignment_id, version, source, status}'

curl --fail-with-body --silent --show-error \
  "$API/assignments/$ASSIGNMENT_ID/submissions/me?limit=50&offset=0" \
  -H "Authorization: Bearer $STUDENT_TOKEN" | jq
```

非 structured 提交必须有非空 `content_text` 且 `content_json` 为 `null`。每次 POST 返回 `201` 并追加版本；学生只能读取自己的版本。

## 教师汇总

```bash
SUMMARY=$(
  curl --fail-with-body --silent --show-error \
    "$API/assignments/$ASSIGNMENT_ID/summary?limit=100&offset=0" \
    -H "Authorization: Bearer $TEACHER_TOKEN"
)
printf '%s' "$SUMMARY" | jq '{
  assignment_id,
  total_students,
  submitted_students,
  missing_students,
  pending_evaluation,
  pending_review,
  reviewed,
  failed,
  students
}'
```

汇总包含 active 学生和每人的最新提交/评估/报告状态；不会把教师 rubric 暴露给学生端。

## 发起评估与查询状态

先定义一个有界轮询函数。它只在 job 为 `succeeded` 时返回 0；读取错误、未知状态、`failed/cancelled` 或超时都返回非零：

```bash
wait_for_job() {
  local job_id=$1
  local wait_seconds=120
  local deadline=$(( $(date +%s) + wait_seconds ))
  local job_response status remaining request_timeout sleep_seconds

  while :; do
    remaining=$(( deadline - $(date +%s) ))
    if [ "$remaining" -le 0 ]; then
      printf '等待评估任务超时（%s 秒）。\n' "$wait_seconds" >&2
      return 1
    fi
    request_timeout=5
    if [ "$remaining" -lt "$request_timeout" ]; then
      request_timeout=$remaining
    fi
    if ! job_response=$(
      curl --fail-with-body --silent --show-error \
        --connect-timeout 2 --max-time "$request_timeout" \
        "$API/evaluation-jobs/$job_id" \
        -H "Authorization: Bearer $TEACHER_TOKEN"
    ); then
      if [ "$(date +%s)" -ge "$deadline" ]; then
        printf '等待评估任务超时（%s 秒）。\n' "$wait_seconds" >&2
      else
        printf '读取评估任务失败；停止轮询。\n' >&2
      fi
      return 1
    fi
    if ! status=$(printf '%s' "$job_response" | jq -er '.status'); then
      printf '评估任务响应缺少有效 status；停止轮询。\n' >&2
      return 1
    fi

    case "$status" in
      succeeded)
        printf '%s' "$job_response" | jq '{id, status, provider, model}'
        return 0
        ;;
      failed|cancelled)
        printf '%s' "$job_response" |
          jq '{id, status, error_code, error_message}' >&2
        printf '评估任务以安全终态 %s 结束。\n' "$status" >&2
        return 1
        ;;
      queued|running)
        ;;
      *)
        printf '评估任务返回未知状态：%s\n' "$status" >&2
        return 1
        ;;
    esac

    remaining=$(( deadline - $(date +%s) ))
    if [ "$remaining" -le 0 ]; then
      printf '等待评估任务超时（%s 秒）。\n' "$wait_seconds" >&2
      return 1
    fi
    sleep_seconds=2
    if [ "$remaining" -lt "$sleep_seconds" ]; then
      sleep_seconds=$remaining
    fi
    sleep "$sleep_seconds"
  done
}
```

批量评估只排每名学生的最新 submitted 版本：

```bash
BATCH=$(
  curl --fail-with-body --silent --show-error \
    -X POST "$API/assignments/$ASSIGNMENT_ID/evaluations" \
    -H "Authorization: Bearer $TEACHER_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"reason":"initial"}'
)
printf '%s' "$BATCH" | jq '{batch_id, queued, skipped, job_ids}'
BATCH_JOB_IDS=$(
  printf '%s' "$BATCH" |
    jq -er '.job_ids | if length > 0 then .[] else error("no evaluation jobs") end'
) || exit 1
while IFS= read -r BATCH_JOB_ID; do
  wait_for_job "$BATCH_JOB_ID" || exit 1
done <<< "$BATCH_JOB_IDS"
```

也可以只评估一个提交：

```bash
JOB=$(
  curl --fail-with-body --silent --show-error \
    -X POST "$API/submissions/$SUBMISSION_ID/evaluations" \
    -H "Authorization: Bearer $TEACHER_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"reason":"initial"}'
)
JOB_ID=$(printf '%s' "$JOB" | jq -er '.id')
printf '%s' "$JOB" | jq '{id, submission_id, status, provider, model, reason}'

# 只有 succeeded 返回 0；失败、取消、读取错误或约 120 秒超时均非零退出。
wait_for_job "$JOB_ID" || exit 1
```

请求返回 `202`。轮询通常每 2 秒一次，每次 HTTP 请求最多 5 秒；接近 120 秒总 deadline 时，函数会收紧最后一次请求与 sleep 的时限。worker 成功后状态变为 `succeeded`；`failed/cancelled` 只打印安全的 `error_code/error_message` 并返回非零，超时同样返回非零。初评请求是幂等的，若该 submission version 已有 initial job，重复请求返回同一 job。

上一段只有在任务为 `succeeded` 时才会继续。随后读取该提交的报告，并提取最新版本：

```bash
REPORTS=$(
  curl --fail-with-body --silent --show-error \
    "$API/submissions/$SUBMISSION_ID/reports?limit=50&offset=0" \
    -H "Authorization: Bearer $TEACHER_TOKEN"
)
REPORT_ID=$(printf '%s' "$REPORTS" | jq -er '.[0].id')
printf '%s' "$REPORTS" | jq '.[0] | {
  id, version, origin, completeness, correctness,
  major_issues, suggestions, score, grade, confidence,
  limitations, validation_status, review_status
}'
```

## 教师确认、修改与重新评估

确认和修改都要求目标是当前 `proposed` 报告，因此它们是二选一的审核分支。以下用种子作业的学生乙报告做确认，用学生甲报告做修改与重评：

```bash
SEED_ASSIGNMENT_ID=$(
  curl --fail-with-body --silent --show-error \
    "$API/assignments?limit=100&offset=0" \
    -H "Authorization: Bearer $TEACHER_TOKEN" |
  jq -er '.[] | select(.code == "HW-0001") | .id'
)
SEED_SUMMARY=$(
  curl --fail-with-body --silent --show-error \
    "$API/assignments/$SEED_ASSIGNMENT_ID/summary?limit=100&offset=0" \
    -H "Authorization: Bearer $TEACHER_TOKEN"
)

CONFIRM_SUBMISSION_ID=$(printf '%s' "$SEED_SUMMARY" | jq -er \
  '.students[] | select(.username == "student2") | .latest_submission.id')
CONFIRM_REPORT_ID=$(
  curl --fail-with-body --silent --show-error \
    "$API/submissions/$CONFIRM_SUBMISSION_ID/reports?limit=50&offset=0" \
    -H "Authorization: Bearer $TEACHER_TOKEN" |
  jq -er '.[0].id'
)
curl --fail-with-body --silent --show-error \
  -X POST "$API/reports/$CONFIRM_REPORT_ID/confirm" \
  -H "Authorization: Bearer $TEACHER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"comment":"字段完整，教师确认。"}' | jq '{id, review_status, request_id}'

MODIFY_SUBMISSION_ID=$(printf '%s' "$SEED_SUMMARY" | jq -er \
  '.students[] | select(.username == "student1") | .latest_submission.id')
SOURCE_REPORT_ID=$(
  curl --fail-with-body --silent --show-error \
    "$API/submissions/$MODIFY_SUBMISSION_ID/reports?limit=50&offset=0" \
    -H "Authorization: Bearer $TEACHER_TOKEN" |
  jq -er '.[0].id'
)
MODIFIED=$(
  curl --fail-with-body --silent --show-error \
    -X PATCH "$API/reports/$SOURCE_REPORT_ID" \
    -H "Authorization: Bearer $TEACHER_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{
      "score": 88,
      "comment": "教师复核后调整分数；大幅改分已说明原因。"
    }'
)
MODIFIED_REPORT_ID=$(printf '%s' "$MODIFIED" | jq -er '.id')
printf '%s' "$MODIFIED" | jq '{id, source_report_id, origin, version, score, grade, review_status}'

RETRY_JOB=$(
  curl --fail-with-body --silent --show-error \
    -X POST "$API/reports/$MODIFIED_REPORT_ID/reevaluate" \
    -H "Authorization: Bearer $TEACHER_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"comment":"基于教师修订重新评估。"}'
)
RETRY_JOB_ID=$(printf '%s' "$RETRY_JOB" | jq -er '.id')
printf '%s' "$RETRY_JOB" | jq '{id, source_report_id, reason, status, request_id}'

curl --fail-with-body --silent --show-error \
  "$API/reports/$MODIFIED_REPORT_ID/workspace" \
  -H "Authorization: Bearer $TEACHER_TOKEN" |
jq '{current_report_id, selected_report, timeline, reevaluation_job}'
```

修改创建新 `origin=teacher` 报告并保留旧版；≥10 分的变化必须提供非空 comment。重新评估返回 `202` 和 `reason=manual_retry` 的 job，`source_report_id` 绑定发起版本。

## Mattermost demo binding

需要 API 以 development/test 启动且 `.env` 设置了 `MATTERMOST_DEMO_SETUP_KEY`：

```bash
DEMO_SETUP_KEY=replace-with-random-demo-binding-key
MM_TEACHER_ID=teacher0000000000000000000
MM_STUDENT_ID=student0000000000000000000

curl --fail-with-body --silent --show-error \
  -X POST "$API/integrations/mattermost/demo-bindings" \
  -H 'Content-Type: application/json' \
  -H "X-Demo-Setup-Key: $DEMO_SETUP_KEY" \
  -d "{\"local_username\":\"teacher\",\"mattermost_user_id\":\"$MM_TEACHER_ID\",\"mattermost_username\":\"teacher-mm\"}" | jq

curl --fail-with-body --silent --show-error \
  -X POST "$API/integrations/mattermost/demo-bindings" \
  -H 'Content-Type: application/json' \
  -H "X-Demo-Setup-Key: $DEMO_SETUP_KEY" \
  -d "{\"local_username\":\"student1\",\"mattermost_user_id\":\"$MM_STUDENT_ID\",\"mattermost_username\":\"student1-mm\"}" | jq
```

真实 `make demo-full` 会自动使用 Mattermost 返回的不可变 User ID 完成四个绑定，通常不需要手工调用。

## 全部 Mattermost Slash Command 的 curl

这些请求模拟 Mattermost 的 URL-encoded POST。`MM_COMMAND_TOKEN` 必须与 API 的 `MATTERMOST_COMMAND_TOKEN` 一致，并且不能放在 URL：

```bash
MM_COMMAND_TOKEN=0123456789abcdefghijklmnop
MM_TEAM_ID=team0000000000000000000000
MM_CHANNEL_ID=channel0000000000000000000

mm_command() {
  actor_id=$1
  actor_name=$2
  command_text=$3
  trigger_id=$4
  curl --fail-with-body --silent --show-error \
    -X POST "$API/integrations/mattermost/commands" \
    -H 'Content-Type: application/x-www-form-urlencoded; charset=utf-8' \
    --data-urlencode "token=$MM_COMMAND_TOKEN" \
    --data-urlencode "team_id=$MM_TEAM_ID" \
    --data-urlencode 'team_domain=course' \
    --data-urlencode "channel_id=$MM_CHANNEL_ID" \
    --data-urlencode 'channel_name=course-home' \
    --data-urlencode "user_id=$actor_id" \
    --data-urlencode "user_name=$actor_name" \
    --data-urlencode 'command=/hw' \
    --data-urlencode "text=$command_text" \
    --data-urlencode "trigger_id=$trigger_id" \
    --data-urlencode 'response_url=http://127.0.0.1:8065/hooks/response' | jq
}

# help（教师或学生）
mm_command "$MM_TEACHER_ID" teacher-mm 'help' doc-help-01

# publish（仅教师；截止时间按 Asia/Shanghai）
mm_command "$MM_TEACHER_ID" teacher-mm \
  'publish --title "Mattermost 验收作业" --due "2098-12-31 23:59" --question "说明最短路算法" --notes "请写复杂度"' \
  doc-publish-01

# list / show（教师和学生均可；学生只见 published/closed）
mm_command "$MM_STUDENT_ID" student1-mm 'list' doc-list-01
mm_command "$MM_STUDENT_ID" student1-mm 'show HW-0001' doc-show-01

# submit（仅学生，生成新的 Mattermost 来源版本）
mm_command "$MM_STUDENT_ID" student1-mm \
  'submit HW-0001 --text "使用优先队列选择最短距离顶点并松弛相邻边"' \
  doc-submit-01

# summary / evaluate（仅教师）
mm_command "$MM_TEACHER_ID" teacher-mm 'summary HW-0001' doc-summary-01
mm_command "$MM_TEACHER_ID" teacher-mm 'evaluate HW-0001' doc-evaluate-01
```

每个 `trigger_id` 应唯一。用完全相同的请求重放时，系统返回第一次持久化的响应并保证一次业务效果。`evaluate` 只写 job/outbox，因此响应是 `评估批次 <UUID>: queued=N, skipped=N`，不是同步报告正文。

## Interactive Action 说明

不要手工伪造按钮 callback：签名必须由报告卡片生成，并绑定真实 `delivery_id`、收件人、私聊频道和 post ID。点击卡片按钮后，Mattermost 会向 `/api/v1/integrations/mattermost/actions` 发送 JSON；服务端先验 HMAC，再核对已投递 outbox。三个 action 是 `confirm`、`reevaluate`、`openreport`，其中打开报告返回 `/teacher/reports/{report_id}`。
