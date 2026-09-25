# CLAUDE.md

@AGENTS.md

## Claude Code 补充

- 上面 AGENTS.md 里的会话规则对 Claude Code 同样强制：**每次会话开始前至少用 Notion MCP 读一次草案，结束前至少写一次 Notion。**
- Notion MCP 工具在 Claude Code 中是 deferred tool，先用 `ToolSearch`（如 `select:mcp__notion__notion-fetch,mcp__notion__notion-update-page`）加载，再调用。
- 读主页：`mcp__notion__notion-fetch`，id 用 `https://app.notion.com/p/3e522e543e588054b2b7f04eabab5a08`。
- 写正文或属性：`mcp__notion__notion-update-page`；新建平台子页面：`mcp__notion__notion-create-pages`。
