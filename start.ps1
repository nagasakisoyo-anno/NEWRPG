# ========================================
#   TRPG Agent 一键启动脚本
#   用法: .\start.ps1
# ========================================

$ErrorActionPreference = "Stop"
$originalEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  TRPG Agent 启动器" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 检查 Python
try {
    $pythonVersion = python --version 2>&1
    Write-Host "[✓] $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "[✗] 未找到 Python，请先安装 Python 3.13+" -ForegroundColor Red
    Read-Host "按回车键退出"
    exit 1
}

# 检查 Node.js
try {
    $nodeVersion = node --version 2>&1
    Write-Host "[✓] Node.js $nodeVersion" -ForegroundColor Green
} catch {
    Write-Host "[提示] 未找到 Node.js，尝试自动安装..." -ForegroundColor Yellow
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "正在通过 winget 安装 Node.js LTS..." -ForegroundColor Cyan
        winget install OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[✓] Node.js 安装成功，请重新运行此脚本" -ForegroundColor Green
        } else {
            Write-Host "[✗] Node.js 安装失败，请手动安装: https://nodejs.org" -ForegroundColor Red
        }
    } else {
        Write-Host "[✗] 未找到 Node.js 且 winget 不可用，请手动安装: https://nodejs.org" -ForegroundColor Red
    }
    Read-Host "按回车键退出"
    exit 1
}

# 从 .env 文件加载 API Key（如果环境变量未设置）
if ((-not $env:DEEPSEEK_API_KEY) -and (Test-Path ".env")) {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*DEEPSEEK_API_KEY\s*=\s*(.+)\s*$') {
            $env:DEEPSEEK_API_KEY = $matches[1].Trim()
        }
    }
}

# 检查 API Key
if (-not $env:DEEPSEEK_API_KEY) {
    Write-Host "[提示] 未设置 DEEPSEEK_API_KEY，请在 .env 文件中配置或设置环境变量" -ForegroundColor Yellow
    $apiKey = Read-Host "请输入你的 DeepSeek API Key"
    if (-not $apiKey) {
        Write-Host "[错误] API Key 不能为空" -ForegroundColor Red
        Read-Host "按回车键退出"
        exit 1
    }
    $env:DEEPSEEK_API_KEY = $apiKey
    Write-Host "[✓] API Key 已设置（本次会话有效）" -ForegroundColor Green
} else {
    Write-Host "[✓] DEEPSEEK_API_KEY 已设置" -ForegroundColor Green
}

Write-Host ""

# ---- 解析 Python：conda 环境 trpg > .venv > py 启动器 ----
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
$pyPrefix = @()
if ($env:TRPG_PYTHON -and (Test-Path $env:TRPG_PYTHON)) {
    $pyExe = $env:TRPG_PYTHON
} elseif (Test-Path ".venv\Scripts\python.exe") {
    $pyExe = (Resolve-Path ".venv\Scripts\python.exe").Path
} else {
    # Ask conda where the environment lives instead of hardcoding one machine's path.
    $pyExe = Find-CondaEnvPython "trpg"
}
if (-not $pyExe) {
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        $pyExe = "py"; $pyPrefix = @("-3")
    } elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
        $pyExe = "python"
    }
}

function Invoke-Py {
    param([Parameter(ValueFromRemainingArguments = $true)] $Rest)
    & $pyExe @pyPrefix @Rest
}

if (-not $pyExe -or -not ((Invoke-Py --version 2>&1 | Out-String).Trim())) {
    Write-Host "[X] 未找到可用的 Python，请安装 Python 3.11+ : https://www.python.org/downloads/" -ForegroundColor Red
    Read-Host "按回车键退出"
    exit 1
}


# 安装 Python 依赖
Write-Host "[1/4] 安装 Python 依赖 (首次较慢)..." -ForegroundColor Cyan
Invoke-Py -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] Python 依赖安装失败，请检查网络后重试" -ForegroundColor Red
    Read-Host "按回车键退出"
    exit 1
}
Write-Host "[OK] Python 依赖就绪" -ForegroundColor Green

# 安装前端依赖
Write-Host "[2/4] 安装前端依赖..." -ForegroundColor Cyan
Push-Location web
try {
    npm install --silent 2>$null
    Write-Host "[✓] 前端依赖就绪" -ForegroundColor Green
} catch {
    Write-Host "[!] 前端依赖安装失败，将继续启动..." -ForegroundColor Yellow
}
Pop-Location

# 构建前端
Write-Host "[3/4] 构建前端..." -ForegroundColor Cyan
Push-Location web
try {
    npm run build 2>$null | Out-Null
    Write-Host "[✓] 前端构建完成" -ForegroundColor Green
} catch {
    Write-Host "[!] 前端构建失败，请手动运行 'cd web && npm run build'" -ForegroundColor Yellow
}
Pop-Location

# 启动服务
Write-Host ""
Write-Host "[4/4] 启动服务..." -ForegroundColor Cyan
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  浏览器打开: http://localhost:8000" -ForegroundColor Green
Write-Host "  按 Ctrl+C 可停止服务" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

if (-not $env:HF_ENDPOINT) { $env:HF_ENDPOINT = "https://hf-mirror.com" }
Invoke-Py -m trpg_agent.api_server
