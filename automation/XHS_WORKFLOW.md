# 每日影视简报 → 小红书工作流

## 工作流

1. 每日影视简报先完成抓取并更新 `generated/latest.json`。
2. 只选择北京时间“前一天”发布的条目；没有昨日条目时不发布，避免用旧闻凑数。
3. 运行 `xhs_generate.py`，生成固定 3:4 封面、逐条信息卡、文案和发布清单。
4. 自动发布任务读取 `manifest.json`，检查摘要指纹是否已经发布。
5. 在已登录的小红书创作服务平台上传图片、填写标题和文案，准备成待发布草稿。
6. 最终发布属于对外内容发布动作，任务在此提醒用户确认；确认后点击发布，并用 `xhs_mark_published.py` 记录笔记链接。遇到登录、验证码、图片上传失败或页面结构变化时立即停止并通知。

## 本地生成

使用 Codex 桌面内置 Python（已经包含 Pillow）：

```bash
/Users/shuaisun/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 automation/xhs_generate.py
```

指定日期预览；`--allow-no-yesterday` 只应用于检查版式，不应用于正式发布：

```bash
/Users/shuaisun/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 automation/xhs_generate.py --publish-date 2026-09-11 --allow-no-yesterday
```

输出目录：`output/xhs/YYYY-MM-DD/`。

## 发布规则

- 封面固定使用“一觉醒来，影视圈发生了什么？”栏目标题。
- 状态在图片底部仅标“已确认”或“待确认”，不增加警告图形或解释性文案。
- 演员名只从来源中明确出现的“主演/领衔主演/演员阵容”字段抽取；没有可靠演员信息时不补写。
- 文案保留片名和演员标签，但总话题数限制在 18 个以内。
- 同一 `digest` 只允许成功发布一次。
- 默认只发前一天的内容；无内容、输入过期或发布页异常时跳过并通知。

## 自动发布边界

截至 2026-09，小红书官方账户开放平台的 `write_notes` 权限仍处于规划中，仅特定场景可申请。普通账号只能通过已登录的创作服务平台浏览器会话准备草稿；该方式可能遇到登录过期、验证码和页面改版，任务必须在这些情况下停止并等待人工处理，不能绕过平台验证。最终发布前保留一次用户确认。

如果未来获得官方 `write_notes` 权限，可以把第 5、6 步替换为官方 API，实现无人值守发布；内容生成和去重部分无需修改。
