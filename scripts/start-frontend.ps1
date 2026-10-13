#Requires -Version 5.1
<#
.SYNOPSIS
    启动前端开发服务器（分离进程），等待端口就绪并打印 PID。

.DESCRIPTION
    与 start-backend.ps1 同思路：服务脱离终端生命周期，关掉终端也在跑。
    不直接把 npm 挂在当前会话里——常驻进程会被执行器的超时机制杀掉
    （后端那次 mvn spring-boot:run 就是 900 秒后被终止，见 README）。

    为什么不用 -RedirectStandardOutput/-RedirectStandardError：
        子进程会继承 PowerShell 的管道句柄，脚本自己退不出、最后被超时杀
        （start-backend.ps1 注释里记的是 java 的表现，npm 同理）。
        vite 的日志直接看终端即可，需要留档时再手动重定向。

.PARAMETER Port
    端口，默认 5173，与 vite.config.js 一致。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start-frontend.ps1
#>
[CmdletBinding()]
param(
    [int]$Port = 5173,
    [switch]$Force     # 端口已被占用时先停掉旧进程
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = Split-Path -Parent $PSScriptRoot
$Front = Join-Path $Root "frontend"

function Fail([string]$Msg) { Write-Host "[x] $Msg" -ForegroundColor Red; exit 1 }

Write-Host "==== 前端环境检查 ====" -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $Front "node_modules"))) {
    Write-Host "  依赖未安装，先执行 npm install ..."
    Push-Location $Front
    try { & npm install } finally { Pop-Location }
}
if (-not (Test-Path (Join-Path $Front "package.json"))) { Fail "找不到 frontend\package.json" }
Write-Host "  node_modules  OK"

# 端口占用检查：默认不抢占，避免误杀别的进程；-Force 时才停。
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    $pid0 = ($listen | Select-Object -First 1).OwningProcess
    if (-not $Force) {
        Write-Host "  端口 $Port 已被占用（PID $pid0）。已在运行，直接用 http://localhost:$Port 即可。" -ForegroundColor Yellow
        Write-Host "  要强制重启：scripts\start-frontend.ps1 -Force"
        exit 0
    }
    Write-Host "  停掉占用端口的旧进程 PID $pid0 ..."
    Stop-Process -Id $pid0 -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

Write-Host "`n==== 启动 vite ====" -ForegroundColor Cyan
$proc = Start-Process -FilePath "npm.cmd" `
    -ArgumentList "run", "dev" `
    -WorkingDirectory $Front `
    -WindowStyle Hidden `
    -PassThru

# 等待端口就绪：vite 冷启动通常 2~5 秒
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        $ready = $true
        break
    }
    if ($proc.HasExited) { Fail "npm 进程已退出，代码 $($proc.ExitCode)——多半是端口被占或依赖缺失" }
}

if (-not $ready) { Fail "等待 20 秒后端口 $Port 仍未就绪" }

$owner = (Get-NetTCPConnection -LocalPort $Port -State Listen | Select-Object -First 1).OwningProcess
Write-Host ""
Write-Host "  OK  PID=$owner  http://localhost:$Port" -ForegroundColor Green
Write-Host "  代理: /api -> http://localhost:8080 （后端未启动时接口会失败）"
Write-Host "  停止: Stop-Process -Id $owner"
Write-Host ""
try {
    $r = Invoke-WebRequest "http://localhost:$Port" -TimeoutSec 5 -UseBasicParsing
    Write-Host "  HTTP $($r.StatusCode)  首页可达" -ForegroundColor Green
} catch {
    Write-Host "  [!] 首页探测失败：$($_.Exception.Message.Split([Environment]::NewLine)[0])" -ForegroundColor Yellow
}
