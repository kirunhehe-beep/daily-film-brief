# 零人工审批更新管线

这套管线的原则不是“自动相信”，而是“自动发布可追溯证据，并把证据强度展示给读者”。

`sources.json` 是唯一的信源白名单。每个启用来源必须提供原始链接，并声明它的类型：`official`、`established_media` 或 `industry_data`。程序不会把单一媒体报道标成官方确认；只有两个不同配置来源的规范化标题完全一致，才会标记为“多源一致”。

运行：

```bash
python3 automation/brief_pipeline.py --sources automation/sources.json --output generated/latest.json
```

输出中的 `errors` 必须进入监控：来源抓取失败时，程序照常产出其他来源，但会记录失败，不会伪造“覆盖正常”。

目前提供的是采集、去重与证据分层基础层。要成为全自动正式发布，还需补两项配置：

1. 为中国内地、港澳台、海外和行业数据分别补入可机器读取、长期稳定的白名单来源。
2. 在 GitHub Actions 或 Cloudflare Worker Cron 中定时运行采集与渲染，并使用 Cloudflare API Token 部署到现有 `daily-film-brief` Pages 项目。

语言模型如用于翻译或摘要，只能改写来源已有信息；渲染页必须保留原始链接、证据等级和生成时间。
