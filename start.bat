@echo off
chcp 65001 >nul
echo ========================================
echo   TRPG Agent 一键启动
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.13+
    pause
    exit /b 1
)

REM 检查 Node.js
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 未找到 Node.js，尝试自动安装...
    where winget >nul 2>&1
    if %errorlevel% equ 0 (
        echo 正在通过 winget 安装 Node.js LTS...
        winget install OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
        if %errorlevel% equ 0 (
            echo [✓] Node.js 安装成功，请重新运行此脚本
        ) else (
            echo [错误] Node.js 安装失败，请手动安装: https://nodejs.org
        )
    ) else (
        echo [错误] 未找到 Node.js 且 winget 不可用，请手动安装: https://nodejs.org
    )
    pause
    exit /b 1
)

REM 从 .env 文件加载 API Key（如果环境变量未设置）
if "%DEEPSEEK_API_KEY%"=="" (
    if exist ".env" (
        for /f "usebackq tokens=1,2 delims==" %%a in (".env") do (
            if "%%a"=="DEEPSEEK_API_KEY" set "DEEPSEEK_API_KEY=%%b"
        )
    )
)

REM 检查 API Key
if "%DEEPSEEK_API_KEY%"=="" (
    echo [提示] 未设置 DEEPSEEK_API_KEY，请在 .env 文件中配置或设置环境变量
    set /p API_KEY=请输入你的 DeepSeek API Key:
    if "!API_KEY!"=="" (
        echo [错误] API Key 不能为空
        pause
        exit /b 1
    )
    set DEEPSEEK_API_KEY=!API_KEY!
)

echo [1/5] 安装 Python 依赖...
set "PY="
if defined TRPG_PYTHON if exist "%TRPG_PYTHON%" set "PY=%TRPG_PYTHON%"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
REM 通过 conda 自身定位环境，避免写死某台机器的安装路径
if not defined PY for /f "delims=" %%P in ('conda run -n trpg python -c "import sys;print(sys.executable)" 2^>nul') do if exist "%%P" set "PY=%%P"
if not defined PY set "PY=python"
%PY% -m pip install -q -r requirements.txt >nul 2>&1
if %errorlevel% neq 0 (
    echo [警告] pip install 失败，尝试继续...
)

echo [2/5] 安装前端依赖...
cd web
call npm install >nul 2>&1
if %errorlevel% neq 0 (
    echo [警告] npm install 失败，尝试继续...
)
cd ..

echo [3/5] 构建前端...
cd web
call npm run build >nul 2>&1
cd ..

echo [4/5] 启动后端服务...
echo [提示] 按 Ctrl+C 可停止服务
echo.
echo ========================================
echo   浏览器打开: http://localhost:8000
echo ========================================
echo.

set HF_ENDPOINT=https://hf-mirror.com
%PY% -m trpg_agent.api_server

pause
