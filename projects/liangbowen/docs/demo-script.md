# 15–20 分钟验收演示脚本

演示前先按 [README](../README.md) 准备 Docker 演示与宿主 E2E 的两组依赖。fresh clone 的预演命令是：

```bash
cp .env.example .env
make install
make install-frontend
make e2e-install
make e2e && test -f artifacts/acceptance/playwright/index.html
make demo
make health
```

只有 `make e2e && test -f ...` 整条命令返回 0，才把该 HTML 称为本次代码生成的浏览器证据；失败时保留终端错误并使用下方不夸大的降级路径，不能把可能残留的旧报告说成刚刚通过。随后在四个浏览器 Profile 中分别登录教师、student1、student2、student3。准备两条业务路径：新建作业用于展示真实闭环，种子作业 `HW-0001` 用于稳定展示 A/C/D 三份报告。

## 00:00–02:00 目标、角色与架构

- **账号**：`teacher / Teacher123!`。
- **页面/命令**：打开 <http://localhost:8080/login>，登录后进入“教师评阅工作台”；另开 [architecture.md](architecture.md) 的组件图。
- **准确操作**：输入教师账号，点击“登录并进入工作台”；指出顶部“首批作业 / 待评估 / 待审核 / 失败任务”四项统计；用架构图依次指向 Nginx、FastAPI、PostgreSQL、Redis/Celery、Provider、可选 Mattermost。
- **可见状态**：教师身份 `@teacher`；作业登记中存在 `HW-0001 · 图的最短路径`；架构图显示 Web 与 Mattermost 进入同一 API/数据库闭环。
- **讲解词**：“这不是一个只会调模型的页面；发布、提交、队列、报告版本和教师审核都以数据库证据为准，AI 只负责生成待审核的基础评估。”

## 02:00–05:00 教师发布作业

- **账号**：`teacher`。
- **页面/命令**：教师工作台，点击“发布新作业”。
- **准确操作**：
  1. 作业标题填“验收：Dijkstra 最短路”；
  2. 题目内容填“说明 Dijkstra 算法的核心思想、复杂度与适用条件。”；
  3. 学生说明填“请给出关键松弛步骤。”；
  4. 截止时间选择未来时间；
  5. 分别输入“说明非负权限制”“分析优先队列复杂度”，每次点击“添加要点”；
  6. 教师评分注意事项填“以概念正确和边界完整为准。”；
  7. 点击“保存作业草稿”，在新增卡片记录其 `HW-xxxx` 编号；
  8. 点击卡片上的“发布 HW-xxxx”。
- **可见状态**：先出现“草稿已保存，可在作业登记中确认后发布”，随后卡片状态变为“已发布”，顶部统计同步更新。
- **讲解词**：“发布被拆成保存草稿和显式发布两个状态转换，截止时间与 rubric 都进入后续 Agent 输入，但学生接口看不到教师 rubric。”

## 05:00–07:00 三名学生提交

- **账号**：三个预登录 Profile：`student1`、`student2`、`student3`，密码均为 `Student123!`。
- **页面/命令**：每个学生的“我的作业”页，点击新作业卡片的“进入作业”。
- **准确操作**：依次粘贴并点击“提交答案”：
  - student1：“Dijkstra 只适用于非负权图；每轮取暂定距离最小顶点并松弛出边，邻接表加二叉堆为 O((V+E)logV)。”
  - student2：“从起点开始，每轮选择当前距离最小且未访问的顶点，再更新相邻点距离。”
  - student3：“每轮选择边权最大的边，就能处理负权边和负环。”
- **可见状态**：每个页面显示“答案已提交为第 1 版”，提交历史新增“第 1 版”；答案仍留在编辑器中，可继续提交为第 2 版。
- **讲解词**：“提交采用追加版本而不是覆盖；Web 和 Mattermost 都调用同一个提交服务，教师汇总只取每名学生的最新有效版本。”

## 07:00–10:00 汇总与 Agent 批量评估

- **账号**：切回 `teacher`。
- **页面/命令**：打开刚创建的作业标题，进入作业详情。
- **准确操作**：确认汇总表中三名学生均有最新版本；点击“评估全部最新提交”；等待状态从“排队中/评估中”变为“待审核”。必要时刷新页面一次。
- **可见状态**：顶部聚合显示提交 3、缺交 0；按钮反馈批量任务已进入队列；默认 `AGENT_PROVIDER=mock`、`AGENT_MOCK_FIXTURE=partial` 时新建作业会得到确定性的 `72 / C` 待审核报告，重复点击显示 skipped 而不会复制 job。
- **讲解词**：“HTTP 请求只原子写 job 和 outbox，worker 异步评估；幂等 key 把提交 ID、版本和原因绑定起来，因此重复点击不会产生重复初评。”

## 10:00–14:00 三类报告对比

- **账号**：`teacher`。
- **页面/命令**：返回教师工作台，打开种子作业“图的最短路径（HW-0001）”。
- **准确操作**：按汇总表的学生甲、学生乙、学生丙依次点击“打开报告”；比较“评分摘要、答案完整性、正确性判断、主要问题、修改建议、能力边界”；每次用浏览器返回作业详情。
- **可见状态**：
  - 学生甲：`94 / A`，complete + correct；
  - 学生乙：`72 / C`，partial + mostly_correct；
  - 学生丙：`35 / D`，incomplete + incorrect；
  - 三份报告均为 `proposed`，包含限制并明确要求教师审核。
- **讲解词**：“这三份报告由固定种子场景生成，保证验收可重复；字段不只是一句总评，而是完整性、正确性、问题、建议、分数和限制的结构化结果。”

## 14:00–16:30 教师确认、修改与重新评估

- **账号**：`teacher`。
- **页面/命令**：继续使用 `HW-0001` 的报告审阅页。
- **准确操作**：
  1. 打开学生乙报告，在“审核评语（可选）”填“字段完整，教师确认”，点击“确认评估”；观察 `confirmed`；
  2. 返回作业详情，打开学生甲当前报告，点击“修改评估”；
  3. 把评分改为 `88`，在“大幅改分必填”评语中填“教师复核后调整分数，用于展示修订留痕”，点击“保存修改”；
  4. 观察 `REPORT REVIEW / v2`、`88 / B` 和“查看版本 1”；
  5. 填“基于教师修订重新运行 Agent”，点击“重新评估”；等待并打开版本 3。
- **可见状态**：确认动作留下教师与评语；修改后旧 v1 仍在时间线，新 v2 为 teacher origin/modified；重评完成后出现新 Agent v3 与来源 lineage。
- **讲解词**：“确认、修改和重评是三种不同审计动作；修改永远新建版本，不会覆盖 AI 原始报告，重评也明确记录从哪一版发起。”

## 16:30–18:00 Mattermost 接入说明

- **账号**：Mattermost `teacher / Teacher123!`；若容器未启用，则在文档中演示命令与 E2E 证据。
- **页面/命令**：<http://localhost:8065> 的 `data-structures / course-home`，或终端显示 [mattermost-setup.md](mattermost-setup.md)。
- **准确操作**：执行 `/hw list`、`/hw summary HW-0001`；说明学生可执行 `/hw submit HW-0001 --text "答案"`，教师可执行 `/hw evaluate HW-0001`；打开一张 Bot 私信卡片，指出“确认评估 / 重新评估 / 打开报告”。
- **可见状态**：命令只返回临时安全消息或发布结果；summary 显示 total/submitted/missing 与队列/审核数；“打开报告”进入 `/teacher/reports/{id}`。
- **讲解词**：“Slash Token 在访问数据库前常量时间校验，身份授权只信任不可变 Mattermost User ID；按钮又用 HMAC、收件人、频道、帖子和已投递 outbox 做二次绑定。”

## 18:00–20:00 测试证据与能力边界

- **账号**：无需登录；终端/文档。
- **页面/命令**：打开 [test-results.md](test-results.md) 与 [limitations.md](limitations.md)，展示 `artifacts/acceptance/playwright/index.html`。
- **准确操作**：展示预演时 `make e2e && test -f artifacts/acceptance/playwright/index.html` 的成功终端输出，再打开由该次命令生成的报告；指出 12 个必测后端验收用例、13 个 E2E runner 测试、4 个 Playwright 流程和 337 个前端测试；按表格逐行说明六类场景；最后明确 Docker/Mattermost 容器在当前机器未实跑。
- **可见状态**：本次预演命令为 0，Playwright 报告显示 4 passed；测试表含 command、expected、actual、evidence、timestamp；边界文档明确“代码未执行、AI 需教师确认、无 OCR/查重/隐藏测试”。
- **讲解词**：“这份 Playwright 报告是本次预演刚刚由 `make e2e` 生成的；历史表格另带执行时间，我只把真正执行成功的检查称为通过，当前没有 Docker 的环境没有伪造容器结论。”

## 故障降级路径

### Provider 或网络不可用：30 秒内切回种子报告

1. 不等待真实 Provider；保持 `AGENT_PROVIDER=mock`，或直接停止新建作业的实时评估演示。
2. 用教师账号打开工作台 → “图的最短路径” → 三名学生的“打开报告”。
3. 在 30 秒内展示预置 `94/A`、`72/C`、`35/D`，并说明它们由真实 `EvaluationService + EvaluationEngine(MockProvider)` 写入，不是前端硬编码。

### Mattermost 不可用

展示 [mattermost-setup.md](mattermost-setup.md) 的请求字段和签名链路，再展示 `backend/tests/mattermost/` 与 `e2e/tests/mattermost-adapter.spec.ts`。说明本地 E2E 已用 URL-encoded 表单跑通 list/submit/summary 与 REST 状态一致性，但当前机器没有 Docker，真实 Mattermost 容器仍待验证。

### 浏览器或现场端口冲突

先用 `make health` 确认 API。只有预演时 `make e2e && test -f artifacts/acceptance/playwright/index.html` 成功，才能打开该报告并称为本次代码的浏览器证据；若 E2E 失败，则展示失败日志与 [test-results.md](test-results.md) 中带时间戳的历史结果，并明确“本次未复现通过”，再用 [api-examples.md](api-examples.md) 演示 REST。不要临时杀死未知占用进程；更改 `.env` 的 `API_PORT/WEB_PORT` 及对应 URL 后重新启动。

### 演示数据已被修改

若之前排练已确认/修改种子报告，且这些本机数据无需保留，执行：

```bash
docker compose --profile mattermost down -v
make demo
```

该操作永久删除本项目 Compose 数据卷；只在明确无需保留数据时使用。
