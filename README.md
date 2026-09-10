# 每日影视简报

一个面向影视从业者与爱好者的 10 分钟行业简报。静态站点位于 `app/`，部署目标为 Cloudflare Pages 项目 `daily-film-brief`。

## 自动更新

GitHub Actions 会在北京时间 10:10 与 18:00（UTC 02:10 / 10:10）运行：

1. 拉取 `automation/sources.json` 中配置的可追溯信息源；
2. 按信源证据自动标注“官方确认 / 多源一致 / 单源待确认”；
3. 只有中国内地、港澳台、国际、行业观察四个板块均有内容时，才生成页面、归档昨日并发布。

此完整性护栏会让信源不全的任务失败而不发布，避免用单一英文源覆盖完整的线上简报。当前仅接入一个用于管线连通性验证的 Variety RSS；补齐各市场的合规、稳定来源并加入中文结构化处理前，请不要手动触发生产发布。

## GitHub Secrets

在仓库 `Settings → Secrets and variables → Actions` 设置：

- `CLOUDFLARE_API_TOKEN`：Cloudflare API Token，Account / Cloudflare Pages / Edit；
- `CLOUDFLARE_ACCOUNT_ID`：目标 Cloudflare 账户 ID。

工作流将通过 Wrangler 更新现有 Pages 项目，不会创建新的 Pages 项目或改变地址。
