# ========================================
#   TRPG Agent 开发模式 (后端热重载 + 前端 HMR)
#   用法:
#     .\dev.ps1                       # 用 conda 环境 trpg
#     .\dev.ps1 -Python <python.exe>  # 指定解释器
#     .\dev.ps1 -SkipInstall          # 跳过 pip install
# ========================================

param(
    [string]$Python = "",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  TRPG Agent 开发模式 (热更新)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$EnvName = "trpg"

function Find-CondaEnvPython([string]$name) {
    if (-not (Get-Command conda -ErrorAction SilentlyContinue)) { return $null }
    try {
        $raw = (& conda env list --json 2>$null | Out-String)
        $i = $raw.IndexOf('{')
        if ($i -lt 0) { return $null }
        $data = $raw.Substring($i) | ConvertFrom-Json
        foreach ($e in $data.envs) {
            if ((Split-Path $e -Leaf) -eq $name) {
                $p = Join-Path $e "python.exe"
                if (Test-Path $p) { return $p }
            }
        }
    } catch { }
    return $null
}

$pyExe = $null
$pyLabel = ""

if ($Python) {
    if (-not (Test-Path $Python)) {
        Write-Host "[X] -Python 指定的路径不存在: $Python" -ForegroundColor Red
        Read-Host "按回车键退出"; exit 1
    }
    $pyExe = (Resolve-Path $Python).Path; $pyLabel = "(-Python 指定)"
} elseif ($env:TRPG_PYTHON -and (Test-Path $env:TRPG_PYTHON)) {
    $pyExe = $env:TRPG_PYTHON; $pyLabel = "(TRPG_PYTHON 环境变量)"
} elseif (Test-Path ".venv\Scripts\python.exe") {
    $pyExe = (Resolve-Path ".venv\Scripts\python.exe").Path; $pyLabel = "(项目 .venv)"
} else {
    $pyExe = Find-CondaEnvPython $EnvName
    if ($pyExe) { $pyLabel = "(conda 环境 $EnvName)" }
}

if (-not $pyExe) {
    Write-Host "[X] 找不到可用的 Python 环境" -ForegroundColor Red
    Write-Host "    本项目应使用独立的 conda 环境，先创建一次：" -ForegroundColor Yellow
    Write-Host "      conda create -n $EnvName python=3.11 -y" -ForegroundColor Yellow
    Write-Host "      .\dev.ps1" -ForegroundColor Yellow
    Read-Host "按回车键退出"
    exit 1
}

$pyVersion = (& $pyExe -c "import sys;print(sys.version.split()[0])" 2>&1 | Out-String).Trim()
if (-not $pyVersion) {
    Write-Host "[X] 解释器无法运行: $pyExe" -ForegroundColor Red
    Read-Host "按回车键退出"; exit 1
}
Write-Host "[OK] Python $pyVersion $pyLabel" -ForegroundColor Green
Write-Host "     $pyExe" -ForegroundColor DarkGray

if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) {
    Write-Host "[X] 未找到 Node.js，请安装 LTS 版本: https://nodejs.org" -ForegroundColor Red
    Read-Host "按回车键退出"; exit 1
}
Write-Host "[OK] Node.js $((node --version))" -ForegroundColor Green

if ((-not $env:DEEPSEEK_API_KEY) -and (Test-Path ".env")) {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*DEEPSEEK_API_KEY\s*=\s*(.+?)\s*$') {
            $env:DEEPSEEK_API_KEY = $matches[1].Trim('"', "'")
        }
    }
}
if (-not $env:DEEPSEEK_API_KEY) {
    Write-Host "[!] 未设置 DEEPSEEK_API_KEY（.env 或环境变量）" -ForegroundColor Yellow
    Write-Host "    界面能打开、接口能通，但 GM 只会返回兜底文本。" -ForegroundColor Yellow
} else {
    Write-Host "[OK] DEEPSEEK_API_KEY 已设置" -ForegroundColor Green
}

if ($SkipInstall) {
    Write-Host "[1/3] 跳过依赖安装 (-SkipInstall)" -ForegroundColor DarkGray
} else {
    Write-Host "[1/3] 检查 Python 依赖 ..." -ForegroundColor Cyan
    & $pyExe -m pip install -q --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[X] Python 依赖安装失败" -ForegroundColor Red
        Write-Host "    国内可换源重试：" -ForegroundColor Yellow
        Write-Host "    $pyExe -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple" -ForegroundColor Yellow
        Read-Host "按回车键退出"; exit 1
    }
    Write-Host "[OK] Python 依赖就绪" -ForegroundColor Green
}

Write-Host "[2/3] 检查前端依赖 ..." -ForegroundColor Cyan
Push-Location web
try {
    if (-not (Test-Path "node_modules")) {
        npm install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "npm install 失败" }
    }
    Write-Host "[OK] 前端依赖就绪" -ForegroundColor Green
} catch {
    Write-Host "[X] 前端依赖安装失败: $_" -ForegroundColor Red
    Pop-Location
    Read-Host "按回车键退出"; exit 1
}
Pop-Location

if (-not $env:HF_ENDPOINT) { $env:HF_ENDPOINT = "https://hf-mirror.com" }
Write-Host "[OK] HF_ENDPOINT=$env:HF_ENDPOINT" -ForegroundColor Green

Write-Host "[3/3] 启动 Vite + uvicorn ..." -ForegroundColor Cyan
$viteJob = Start-Job -ScriptBlock {
    param($root)
    Set-Location "$root/web"
    npx vite --host 127.0.0.1
} -ArgumentList $PWD.Path

$viteReady = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:5000" -UseBasicParsing -TimeoutSec 2
        $viteReady = $true
        break
    } catch { }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
if ($viteReady) {
    Write-Host "  前端 (HMR):  http://127.0.0.1:5000" -ForegroundColor Green
} else {
    Write-Host "  前端 (HMR):  启动较慢，稍后访问 http://127.0.0.1:5000" -ForegroundColor Yellow
}
Write-Host "  后端 API:    http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "  接口文档:    http://127.0.0.1:8000/docs" -ForegroundColor Green
Write-Host "  Ctrl+C 停止两个服务" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

try {
    & $pyExe -m uvicorn trpg_agent.api_server:app --reload --reload-dir trpg_agent --host 127.0.0.1 --port 8000
} finally {
    Stop-Job -Job $viteJob -ErrorAction SilentlyContinue
    Remove-Job -Job $viteJob -Force -ErrorAction SilentlyContinue
    Write-Host "开发服务已停止" -ForegroundColor Yellow
}
