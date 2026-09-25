# agent-accounts

给 Echo agent 配上自己的账号，让它以自己的身份使用外部平台。第一站：抖音私信。

项目草案、路线图和进展都在 Notion：[Feat：agent自己的账号系统](https://app.notion.com/p/3e522e543e588054b2b7f04eabab5a08)。

## 快速开始

```bash
uv sync
uv run douyin login       # 弹出浏览器，用 agent 专用账号扫码
uv run douyin doctor      # 检查登录态和关键元素（只读，不发送）
uv run douyin sync        # 打开首页拦截私信接口，新消息入库（不点进会话，不会标记已读）
uv run douyin inbox --offline         # 会话列表（去掉 --offline 会先同步）
uv run douyin thread <昵称或conv_id>  # 读本地消息，--json 输出结构化数据
uv run agent-accounts freeze douyin   # 一键冻结，之后一切自动化操作都会被拒绝
```

运行数据（浏览器 profile、SQLite、失败快照）都放在 `~/.agent-accounts/`，可用 `AGENT_ACCOUNTS_HOME` 覆盖，不会进入仓库。
可选配置 `~/.agent-accounts/config.toml`，字段见 `src/agent_accounts/core/config.py`。

## 目录

```
src/agent_accounts/
  core/        config · paths · store(SQLite) · audit · alerts · run · capability · pb(protobuf)
  browser/     session(Playwright 持久化 profile) · locate(多策略定位)
  adapters/
    douyin/    selectors · page · login · doctor · im(接口解析) · store · sync · spike · cli
scripts/       make_douyin_fixtures.py（真实响应 → 脱敏 fixture）
```
