# agent-accounts

给 Echo agent 配上自己的账号，让它以自己的身份使用外部平台。第一站：抖音私信。

项目草案、路线图和进展都在 Notion：[Feat：agent自己的账号系统](https://app.notion.com/p/3e522e543e588054b2b7f04eabab5a08)。

## 快速开始

```bash
uv sync
uv run douyin login       # 弹出浏览器，用 agent 专用账号扫码
uv run douyin doctor      # 检查登录态和关键元素（只读，不发送）
uv run douyin sync        # 打开首页拦截私信接口，新消息入库（不点进会话，不会标记已读）
uv run douyin sync --watch  # 一直同步（间隔同 douyin run），不调模型、不发送
uv run douyin inbox --offline         # 会话列表（去掉 --offline 会先同步）
uv run douyin thread <昵称或conv_id>  # 读本地消息，--json 输出结构化数据
uv run agent-accounts freeze douyin   # 一键冻结，之后一切自动化操作都会被拒绝
```

运行数据（浏览器 profile、SQLite、失败快照）都放在 `~/.agent-accounts/`，可用 `AGENT_ACCOUNTS_HOME` 覆盖，不会进入仓库。
可选配置 `~/.agent-accounts/config.toml`，字段见 `src/agent_accounts/core/config.py`。

## MCP

`uv run agent-accounts mcp` 启动 stdio MCP server，给 Echo 用：

| 工具 | 作用 |
|---|---|
| `douyin_list_conversations` | 会话列表（昵称、是否互关、未读、最后一条） |
| `douyin_recent_messages` | 最近的私信往来；可指定会话（conv_id 或昵称），不指定就是所有会话 |
| `get_persona` / `update_persona` | 读取 / 整体替换人设 `~/.agent-accounts/persona.md` |
| `get_recent` / `update_recent` | 读取 / 整体替换近况 `~/.agent-accounts/recent.md`（可清空） |

- 读取只查本地库，不开浏览器、不标记已读；数据来自最近一次 `douyin sync` / `douyin run`。
  要让 MCP 一直读到最新数据：开着 `douyin run`，或只同步不回复的 `douyin sync --watch`
- 人设和近况都会用于生成自动回复；覆盖前旧版本存到 `~/.agent-accounts/history/`，审计只记字数
- 不提供发送工具，发送仍只走 `douyin run --allow-send` 和 `douyin reply`

**stdio**（本机，由客户端按需拉起，没有端口）：

```bash
claude mcp add agent-accounts -- uv run --directory <仓库路径> agent-accounts mcp
```

**HTTP**（常驻进程，Bearer token 鉴权，默认 `http://127.0.0.1:8765/mcp`，地址在配置 `[mcp]` 段）：

```bash
uv run agent-accounts mcp-token            # 生成 token 写入配置，token 输出到 stdout；--rotate 换新
uv run agent-accounts mcp --http           # 启动；--host / --port 可临时覆盖
claude mcp add --transport http agent-accounts http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <token>"
```

Windows 上生成 token：`powershell -ExecutionPolicy Bypass -File scripts\new-mcp-token.ps1`
（写入配置并复制到剪贴板，默认不显示；`-Rotate` 换新，`-Show` 同时显示）。
`host` 改成非本机地址时是明文 HTTP，只在可信内网或 HTTPS 反向代理 / Tailscale 后面用。

## 目录

```
src/agent_accounts/
  core/        config · paths · store(SQLite) · audit · alerts · run · capability · pb(protobuf)
               persona(人设 / 近况) · reply · guard
  browser/     session(Playwright 持久化 profile) · locate(多策略定位)
  adapters/
    douyin/    selectors · page · login · doctor · im(接口解析) · store · sync · spike · cli
  mcp_server.py  MCP server（stdio）
scripts/       make_douyin_fixtures.py（真实响应 → 脱敏 fixture） · new-mcp-token.ps1（Windows 生成 MCP token）
```
