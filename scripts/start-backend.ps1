# 启动后端（分离进程）
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start-backend.ps1
#
# 为什么是现在这个写法——踩过的坑都记在下面，别改回去：
#   1. Start-Process -RedirectStandardOutput/Error 会让 java 继承本 PowerShell 的
#      stdout/stderr 管道句柄。脚本逻辑跑完了，但管道不 EOF，调用方一直等到被
#      执行器超时杀掉。所以这里**不重定向**，日志交给 Spring 自己写文件。
#   2. 用 `cmd /c start ... | Out-Null` 同样没用，接管道等于把 PowerShell 又挂回
#      cmd 的 stdout。
#   3. CIM/WMI 创建的进程工作目录是 C:\Windows\System32，而 H2 用的是相对路径
#      ./data，结果去 System32 建库、权限失败：
#          Error while creating file ""C:/Windows/System32/data""
#      `cd /d` 和 `--spring.datasource.url=...` 两种修法在 WMI 下都不可靠
#      （后者 URL 里的分号与引号会被 start 转发吃掉）。
#      现行方案是 -Dapp.data.dir=<绝对路径>：-D 排在 -jar 之前，不含分号，
#      不会被 cmd 的引号规则破坏。
#   4. 就绪判据必须是「刚起的这个 PID 自己监听了端口」，不能是「8080 上有人」。
#      旧实例还活着时，端口判据会被旧实例的监听喂饱，脚本就会在**新实例已经
#      撞端口崩掉**的情况下打印 OK。详见下面「端口预检」那段。

[CmdletBinding()]
param(
    # 端口已被旧实例占着时，是否先把旧实例停掉再起新的。
    # 默认不加这个开关：只在预检阶段报错退出，不替用户杀进程。
    [switch]$ForceRestart
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$jdk  = 'C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot'
$jar  = Join-Path $root 'backend\target\insurance-rag-backend-0.1.0.jar'
$data = Join-Path $root 'backend\data'
$port = 8080
$pidFile = Join-Path $root 'backend\backend.pid'
$logFile = Join-Path $root 'backend\backend.log'

# ---- 读日志尾巴 -------------------------------------------------------
# 这里有个混合编码问题，顺序反了就会把最该看的那几行搞成乱码：
#   1. JVM 没显式指定日志字符集时用平台默认（本机 zh-CN = GBK）。application.yml
#      里已显式改成 UTF-8，但**改之前**写进同一个文件的老行还是 GBK。
#   2. 于是 backend.log 是**混合体**：老的 GBK 行 + 新的 UTF-8 行。
#   3. 整份按 UTF-8 严格解码必然失败（卡在任意一行老日志上）；若就此退成
#      GB18030，最近这几行新日志又会被解成「鍏ュ弬鍛...」这种倒着的乱码。
# 所以**逐行**解码：每行先试严格 UTF-8，失败再退 GB18030。老行、新行都对，
# 只读取尾部一段，也不怕日志变大。
function Get-LogTail {
    param([string]$Path, [int]$Tail = 25)
    $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
    try {
        $len = $fs.Length
        if ($len -eq 0) { return @() }
        $window = [int][Math]::Min($len, 262144)
        $null = $fs.Seek(-$window, 'End')
        $buf = New-Object byte[] $window
        $read = $fs.Read($buf, 0, $window)
        if ($read -lt $window) { $buf = $buf[0..($read - 1)] }
    } finally {
        $fs.Close()
    }

    $utf8 = New-Object System.Text.UTF8Encoding($false, $true)   # 遇到非法字节抛异常
    $gbk  = [System.Text.Encoding]::GetEncoding(936)
    $out  = New-Object System.Collections.Generic.List[string]

    # 按字节切行：循环找 0x0A，避开「先整体解码」导致换行识别被编码影响。
    $start = 0
    for ($i = 0; $i -le $buf.Length; $i++) {
        if ($i -ne $buf.Length -and $buf[$i] -ne 10) { continue }
        $n = $i - $start
        if ($n -gt 0 -and $buf[$i - 1] -eq 13) { $n-- }          # 去掉 CR
        if ($n -le 0) {
            $out.Add('')
        } else {
            $lineBytes = $buf[$start..($start + $n - 1)]
            try   { $out.Add($utf8.GetString($lineBytes)) }
            catch { $out.Add($gbk.GetString($lineBytes)) }
        }
        $start = $i + 1
    }
    while ($out.Count -gt 0 -and $out[$out.Count - 1] -eq '') { $out.RemoveAt($out.Count - 1) }
    if ($out.Count -gt $Tail) { $out = $out.GetRange($out.Count - $Tail, $Tail) }
    return $out
}

if (-not (Test-Path $jar)) {
    Write-Error "jar 不存在: $jar`n请先在 backend 目录执行: mvn -DskipTests package"
}
New-Item -ItemType Directory -Force -Path $data | Out-Null

# ---- 注入 Dify 应用 API Key ------------------------------------------------
# Key 存在 Dify 的 api_tokens 表里。手工设环境变量的问题是：换个终端、隔天重启，
# 就忘了设，后端静默降级成「检索服务暂时不可用」。这里直接从库里读。
# 读进内存后只打印前缀，明文既不落盘也不进日志。
$appId = 'f5939e3a-5fd7-43e7-8824-54d6ed1bc8da'
$key = docker exec docker-db_postgres-1 psql -U postgres -d dify -t -A `
        -c "select token from api_tokens where app_id='$appId' order by created_at desc limit 1;"
if ($LASTEXITCODE -ne 0 -or -not $key) {
    Write-Error "读不到 Dify API Key。确认 Dify 容器在跑、且应用 $appId 已创建 API Key。"
}
$key = $key.Trim()
$env:DIFY_APP_API_KEY = $key
Write-Host ("  key  : {0}***（已从 api_tokens 表读入，明文不落盘）" -f $key.Substring(0, 8))

$java = Join-Path $jdk 'bin\java.exe'

# ---- 端口预检 ---------------------------------------------------------
# 踩坑（真实发生过）：这里原来没有预检，直接 Start-Process，然后用「8080 上有没有
# 人在听」当就绪判据。旧实例还活着占着 8080 时，新实例自己撞端口挂掉，日志里写着
# APPLICATION FAILED TO START / Port 8080 was already in use，脚本却因为「8080 在听」
# 打印出 OK PID=<已经死了的进程>。就绪判据必须认「这个 PID 自己监听了端口」，
# 光认端口会把旧实例的监听当成新实例就绪。
$occupied = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($occupied) {
    $owners = @($occupied | Select-Object -ExpandProperty OwningProcess -Unique)
    Write-Host "端口 $port 已被占用：" -ForegroundColor Red
    foreach ($procId in $owners) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        $desc = if ($proc) { $proc.Name + '  ' + $proc.CommandLine } else { '<进程已退出>' }
        Write-Host "    PID=$procId  $desc"
    }
    if (-not $ForceRestart) {
        Write-Host "  确认是要换新实例，加 -ForceRestart 重跑（会先停掉上面这些进程）：" -ForegroundColor Yellow
        Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\start-backend.ps1 -ForceRestart"
        Write-Host "  或手工停：powershell -ExecutionPolicy Bypass -File scripts\stop-backend.ps1" -ForegroundColor Yellow
        exit 2
    }
    Write-Host "  -ForceRestart：停掉旧实例 ..."
    foreach ($procId in $owners) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        if ($proc.Name -ne 'java.exe') {
            Write-Error "端口 $port 被非 java 进程 PID=$procId ($($proc.Name)) 占用，不会自动杀。请自行处理后重跑。"
        }
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    # 端口释放是异步的，等它真的空出来再起
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) { break }
    }
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        Write-Error "旧实例已收到停止信号，但端口 $port 仍未释放，稍后再试。"
    }
    Write-Host "  旧实例已停，端口已释放。"
}

Write-Host "启动后端 ..."
Write-Host "  jar  : $jar"
Write-Host "  log  : $logFile"

# 注意：没有 -RedirectStandardOutput / -RedirectStandardError / 管道。
# 参数用数组传，不用反引号续行——续行符碰上 CRLF/LF 混排会静默失效，
# 表现为「-ArgumentList 被当成独立命令」这种莫名其妙的解析错误。
# 参数位置有讲究，加之前先看清楚：
#   -Dxxx 是 **JVM** 参数，必须在 -jar 之前；
#   --xxx= 是 **Spring 应用**参数，必须在 -jar <jar> 之后。
# 搞反了 JVM 会直接拒绝启动，而且这种失败发生在日志初始化之前，日志里一个字都没有：
#   Unrecognized option: --server.port=8080
#   Error: Could not create the Java Virtual Machine.
$argList = @(
    "-Dapp.data.dir=$data",
    '-jar',
    $jar,
    "--server.port=$port"
)

# 先记下日志当前的状态。失败时要靠它区分「这次启动写了几行」和「这次一行都没写」——
# 后者说明进程死在 Spring 日志初始化之前（JVM 级失败、jar 被占用/损坏），
# 这时把上一次启动的日志尾巴打出来只会误导人。
$logStamp = if (Test-Path $logFile) { (Get-Item $logFile).LastWriteTimeUtc } else { [datetime]::MinValue }

$p = Start-Process -FilePath $java -ArgumentList $argList -WorkingDirectory (Join-Path $root 'backend') -WindowStyle Hidden -PassThru
Set-Content -Path $pidFile -Value $p.Id -Encoding ascii

# 等待端口就绪——判据是「刚起的这个 PID 自己监听了端口」，不是「端口上有人」。
# 顺序也改了：先看进程有没有死，再看端口。原来的顺序在旧实例占着端口时，
# 会先命中端口直接判就绪，进程已经退出都看不见。
$ready = $false
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Milliseconds 500
    if ($p.HasExited) { break }
    $mine = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.OwningProcess -eq $p.Id }
    if ($mine) { $ready = $true; break }
}

if ($ready) {
    Write-Host "  OK  PID=$($p.Id)  http://localhost:$port" -ForegroundColor Green
    Write-Host "  停止: powershell -ExecutionPolicy Bypass -File scripts\stop-backend.ps1"
    exit 0
}

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
$why = if ($p.HasExited) { "进程已退出 (exit=$($p.ExitCode))" } else { '120 秒内端口未就绪' }
Write-Host "  启动失败：$why" -ForegroundColor Red

$logFresh = (Test-Path $logFile) -and ((Get-Item $logFile).LastWriteTimeUtc -gt $logStamp)
if ($logFresh) {
    Write-Host "  日志尾部 ($logFile):" -ForegroundColor Red
    Get-LogTail -Path $logFile -Tail 25 | ForEach-Object { Write-Host "    $_" }
} elseif (Test-Path $logFile) {
    Write-Host "  本次启动没有往 $logFile 写任何内容——进程死在 Spring 日志初始化之前，" -ForegroundColor Red
    Write-Host "  所以下面这份日志是上一次启动的，不代表本次失败原因：" -ForegroundColor Red
    Get-LogTail -Path $logFile -Tail 12 | ForEach-Object { Write-Host "    $_" }
    Write-Host "  本次失败原因要到前台才看得到（JVM 级失败，例如 jar 被占用/损坏/被杀软拦）：" -ForegroundColor Yellow
    Write-Host "    cd backend; & '$java' '-Dapp.data.dir=$data' '-jar' '$jar'" -ForegroundColor Yellow
} else {
    Write-Host "  日志文件不存在：$logFile" -ForegroundColor Red
}
exit 1