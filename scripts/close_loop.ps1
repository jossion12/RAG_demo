#Requires -Version 5.1
<#
.SYNOPSIS
    RAG 调参闭环：一条命令跑完「记录链路 -> 端到端评测 -> 采集漏斗 -> 归因」。

.DESCRIPTION
    以前调参的顺序是：改参数 -> 跑 15 分钟全量评测 -> 看报告。
    报告只能告诉你「引用没兜住期望条款」，答不出**卡在哪一环**，
    于是下一轮要么瞎试参数，要么凭印象猜。

    这个脚本把四步串成固定顺序，且**强制同轮**——这是最容易出错的地方：
    检索侧 trace 和端到端 raw 如果不是同一次调用、期间动过参数，
    会出现「trace 说期望章节进了上下文 / raw 说引用里没有」这种假矛盾，
    报告会把它标成 trace_conflict（本轮实测 9/52，占比不低）。

        1. dump_corpus       拉全库片段快照（切割层判据的底座）
        2. run_iteration     端到端评测（走前端同款链路，15 分钟）
        3. trace_stages      同一批用例的三段对照探针（只检索，约 100 秒）
        4. attribute_funnel  合成漏斗归因报告 + 建议阈值

.PARAMETER Tag
    本轮标签。三份产物都落在这个目录下，便于跨轮对比。

.PARAMETER Resume
    断点续跑。评测已跑过的用例不重发请求（判据改了也能免费重判）。

.EXAMPLE
    .\close_loop.ps1 -Tag r007
    中断后续跑：
    .\close_loop.ps1 -Tag r007 -Resume
    只重跑归因（raw 已有，不重新发请求）：
    .\close_loop.ps1 -Tag r007 -SkipEval -Resume
#>
[CmdletBinding()]
param(
    [string]$Tag = "r007",
    [string]$Python = "C:\Python312\python.exe",
    [switch]$SkipEval,
    [switch]$SkipTrace,
    [switch]$Resume
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runs = Join-Path $Root "eval\runs"
$OutDir = Join-Path $Runs $Tag

# Windows PowerShell 5.1 控制台默认按 GBK 输出，脚本里的中文会显示成乱码。
# 只影响显示、不影响逻辑，但调参时要看报告摘要，乱码会让人误读结论。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-Step([string]$Msg) {
    Write-Host ""
    Write-Host "==== $Msg ====" -ForegroundColor Cyan
}

function Fail([string]$Msg) {
    Write-Host "[x] $Msg" -ForegroundColor Red
    exit 1
}

Write-Step "0/4 前置检查"

if (-not (Test-Path $Python)) { Fail "找不到 python：$Python" }
Write-Host "  python  OK  $Python"

# Dify 与 TEI 都要就绪。TEI 冷启动要 60~90 秒加载模型，没就绪时
# rerank 段会**静默返回 0 条**，整轮 trace 的结论会作废。
try {
    $null = Invoke-WebRequest "http://localhost/v1/datasets" -TimeoutSec 8 -UseBasicParsing -Headers @{ Authorization = "Bearer x" }
} catch {
    if ($_.Exception.Message -notmatch "401|unauthorized") {
        Fail "Dify 没起来（http://localhost 无响应）。先 cd dify\docker && docker compose up -d"
    }
}
Write-Host "  dify    OK"

$teiReady = $false
try {
    $null = Invoke-WebRequest "http://localhost:9999/health" -TimeoutSec 6 -UseBasicParsing
    $teiReady = $true
} catch { }
if ($teiReady) {
    Write-Host "  tei     OK"
} else {
    Write-Host "  [!] TEI rerank 不可达 —— rerank 段会返回 0 条，trace 结论不可用。" -ForegroundColor Yellow
    Write-Host "      等它加载完（60~90 秒）再重跑，或先 docker logs tei-rerank --tail 20" -ForegroundColor Yellow
    Fail "TEI 未就绪，中止（继续跑只会浪费一轮 15 分钟）"
}

Write-Step "1/4 语料快照（切割层判据底座）"
& $Python (Join-Path $Root "scripts\dump_corpus.py")
if ($LASTEXITCODE -ne 0) { Fail "dump_corpus 失败" }

if (-not $SkipEval) {
    Write-Step "2/4 端到端评测（约 15 分钟，52 条）"
    $evalArgs = @((Join-Path $Root "eval\run_iteration.py"), "--tag", $Tag, "--workers", "2")
    if ($Resume) { $evalArgs += "--resume" }
    & $Python @evalArgs
    if ($LASTEXITCODE -ne 0) { Fail "run_iteration 失败" }
} else {
    Write-Step "2/4 跳过端到端评测（-SkipEval）"
}

if (-not $SkipTrace) {
    Write-Step "3/4 三段漏斗采集（约 100 秒，只检索不调 LLM）"
    $traceArgs = @((Join-Path $Root "eval\trace_stages.py"), "--tag", $Tag)
    if ($Resume) { $traceArgs += "--resume" }
    & $Python @traceArgs
    if ($LASTEXITCODE -ne 0) { Fail "trace_stages 失败" }
} else {
    Write-Step "3/4 跳过漏斗采集（-SkipTrace）"
}

Write-Step "4/4 漏斗归因"
& $Python (Join-Path $Root "eval\attribute_funnel.py") --tag $Tag --raw $Tag --params-sweep
if ($LASTEXITCODE -ne 0) { Fail "attribute_funnel 失败" }

Write-Step "完成"
Write-Host "  漏斗报告  $(Join-Path $OutDir 'funnel_report.md')" -ForegroundColor Green
Write-Host "  归因明细  $(Join-Path $OutDir 'attribution.jsonl')" -ForegroundColor Green
Write-Host "  链路轨迹  $(Join-Path $OutDir 'trace.jsonl')" -ForegroundColor Green
Write-Host ""
Write-Host "下一步：看报告里「参数建议」那一节确定该拧哪个旋钮，" -ForegroundColor Yellow
Write-Host "用 scripts\dify_params.py 改完，再跑一轮本脚本对比。" -ForegroundColor Yellow