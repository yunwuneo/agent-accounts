<#
.SYNOPSIS
  生成 HTTP MCP 的 Bearer token，写入 ~/.agent-accounts/config.toml 的 [mcp] 段，并复制到剪贴板。

.DESCRIPTION
  实际的生成和写入由 `agent-accounts mcp-token` 完成（保留配置文件里的其他内容和注释）。
  token 默认不显示在屏幕上，只放进剪贴板；在 MCP 客户端里配置请求头
  Authorization: Bearer <token> 即可。

.PARAMETER Rotate
  已有 token 时换一个新的（旧 token 立即失效，客户端要同步更新）。

.PARAMETER Show
  同时把 token 打印到屏幕上。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\new-mcp-token.ps1
  powershell -ExecutionPolicy Bypass -File scripts\new-mcp-token.ps1 -Rotate
#>
param(
    [switch]$Rotate,
    [switch]$Show
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error '找不到 uv，请先安装：https://docs.astral.sh/uv/'
    exit 1
}

$cmdArgs = @('run', '--directory', $repo, 'agent-accounts', 'mcp-token')
if ($Rotate) { $cmdArgs += '--rotate' }

# stdout 只有 token 一行；提示信息走 stderr，直接显示在终端
$output = & uv @cmdArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "生成失败（退出码 $LASTEXITCODE），配置文件没有改动。" -ForegroundColor Red
    exit $LASTEXITCODE
}
$token = ($output | Select-Object -Last 1).Trim()

Set-Clipboard -Value $token
Write-Host 'token 已写入配置文件，并复制到剪贴板。' -ForegroundColor Green
if ($Show) { Write-Host "token: $token" }

Write-Host ''
Write-Host '启动 HTTP MCP：  uv run agent-accounts mcp --http'
Write-Host '客户端请求头：    Authorization: Bearer <剪贴板里的 token>'
Write-Host '查看配置：        uv run agent-accounts config show'
if ($Rotate) {
    Write-Host '旧 token 已失效；正在运行的 HTTP MCP 需要重启才会用上新 token。' -ForegroundColor Yellow
}
