# 停止后端
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\stop-backend.ps1
#
# 为什么不用「照着启动脚本上次打印的 PID 敲 Stop-Process -Id N」：
# 那个 PID 只在当次启动成功时有效。进程崩了、机器重启了、或者上次启动其实
# 失败（打印的 PID 根本不是监听 8080 的那个），照着敲要么报「找不到进程」，
# 要么误杀。真正的依据是「现在谁在监听 8080」。
#
# 停止策略：先认 pid 文件（start-backend.ps1 写的），pid 文件缺失/失效时
# 回退到端口归属；两种来源都拿不到就说没在跑。端口上的非 java 进程
# 一律不杀——那不是本项目的东西，不替用户做决定。

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$port = 8080
$pidFile = Join-Path $root 'backend\backend.pid'

$targets = @()

if (Test-Path $pidFile) {
    $recorded = (Get-Content $pidFile -Raw).Trim()
    if ($recorded -match '^\d+$') {
        if (Get-Process -Id ([int]$recorded) -ErrorAction SilentlyContinue) { $targets += [int]$recorded }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# pid 文件不可信（手工 java -jar 起的）时，以端口归属为准
$listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
               Select-Object -ExpandProperty OwningProcess -Unique)
foreach ($procId in $listeners) {
    if ($targets -contains $procId) { continue }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if (-not $proc) { continue }
    if ($proc.Name -ne 'java.exe') {
        Write-Host "跳过 PID=$procId（$($proc.Name) 不是 java，不确定是不是本项目的进程，不自动杀）" -ForegroundColor Yellow
        continue
    }
    $targets += $procId
}

if ($targets.Count -eq 0) {
    Write-Host "没有后端在跑（端口 $port 空闲）"
    exit 0
}

foreach ($procId in $targets) {
    Write-Host "停止 PID=$procId ..."
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
}

# 等端口真的空出来，别让 start-backend.ps1 紧接着又撞端口
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) { break }
}

if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "进程已发停止信号，但端口 $port 还没释放，稍等一下再启动。" -ForegroundColor Yellow
    exit 1
}

Write-Host "  已停止，端口 $port 已释放" -ForegroundColor Green
exit 0