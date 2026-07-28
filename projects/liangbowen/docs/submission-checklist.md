# 最终验收与提交清单

勾选只表示仓库内已有可核对证据，不把待在 Docker 机器或课程平台完成的步骤写成通过。验收运行时基线为 `b238a63`；后续文档证据提交不修改该运行时实现。

| # | 要求 | 状态 | 证据 / 缺口 |
| ---: | --- | :---: | --- |
| 1 | 可运行的本地系统 | [x] | [测试结果](test-results.md) 与 [clean-room 摘要](evidence/clean-room-acceptance.json) 记录 `make verify-local` 5 个非 Docker 门禁全部通过，含真实 PostgreSQL/API/Vite/Playwright 闭环；严格验证为 5 passed / 3 blocked。**Docker Compose 和 Nginx 容器未在当前机器实跑。** |
| 2 | Mattermost 连接方式和本地演示凭据已说明 | [x] | [Mattermost 接入文档](mattermost-setup.md) 记录 Webhook/Slash Command、Bot Token、Action Secret、身份绑定与卡片回调；[README](../README.md) 和 `.env.example` 给出本地账号/配置项。真实 Mattermost 10.5 容器待 Docker 环境补验。 |
| 3 | 完整的发布 → 提交 → 汇总 → Agent 评估 → 教师审核链路 | [x] | [`full-flow.spec.ts`](../e2e/tests/full-flow.spec.ts) 实际覆盖教师发布、学生提交、汇总/评估、确认、修改、重评和版本历史；strict Playwright 4/4 通过。 |
| 4 | 三份明显不同的评估报告 | [x] | `HW-0001` 种子数据生成 `94/A`、`72/C`、`35/D`；正式 tracked [`seeded-reports.spec.ts`](../e2e/tests/seeded-reports.spec.ts) 在后续 student1 v2 存在时仍通过教师 UI 打开三份历史报告，断言不同完整性/正确性/诊断并检查页面不泄密；实跑见 [clean-room 摘要](evidence/clean-room-acceptance.json)。 |
| 5 | 六类必测场景 | [x] | `backend/tests/acceptance/` 的 12 个用例覆盖完整、部分正确、明显错误、模糊、Agent 非法/不稳定输出、能力边界；[测试结果](test-results.md) 逐行记录 expected/actual/evidence。 |
| 6 | 诚实的系统能力边界 | [x] | [能力边界](limitations.md) 明确代码不执行、AI 结果需教师确认、无 OCR/查重/隐藏测试；[测试结果](test-results.md) 另列 Docker、Mattermost 容器与真实 Provider 未验项。 |
| 7 | README 足以让他人运行 | [x] | [README](../README.md) 包含前置条件、配置、安装、Docker 与宿主 E2E 路径、账号、常用命令、故障诊断和数据清理；[clean-room 摘要](evidence/clean-room-acceptance.json) 证明只用 tracked files + `.env.example` 可安装并通过非 Docker 门禁。 |
| 8 | 代码已放到课程要求的 `projects/<issued-name>/` | [ ] | **外部待办**：当前仓库路径不是管理员分配的课程目录，且没有可核对的 `COURSE_PROJECT_DIR`；未移动或复制到外部仓库。 |
| 9 | 课程进度表已更新为“已提交” | [ ] | **外部待办**：仓库内无课程进度表的可核对记录，未擅自修改外部状态。 |
| 10 | 已与管理员安排现场演示或替代录屏 | [ ] | **外部待办**：自动技术预演已通过，但未做真人 20 分钟计时、未录屏，也没有管理员安排证据。 |

## 提交人需完成的三项外部动作

1. 在有 Docker/Compose 的干净机器执行 [测试结果](test-results.md#docker-环境补验命令) 所列命令，补验 `make demo`、Nginx 和真实 Mattermost 10.5。
2. 从管理员处确认精确的个人目录名，把已验证提交合并到 `projects/<issued-name>/`，再 push；不要自行猜测或重命名该目录。
3. 更新课程进度表为“已提交”，并与管理员确认现场演示；若需录屏替代，应在截止时间前沟通并保留确认。
