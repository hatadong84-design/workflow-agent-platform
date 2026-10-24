param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

$EnvFile = Join-Path $ProjectRoot "workflow\.env"
$EnvExample = Join-Path $ProjectRoot "workflow\.env.example"
if (-not (Test-Path -LiteralPath $EnvFile)) {
    Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
    Write-Host "已从 workflow\.env.example 创建 workflow\.env（默认 mock 模式）。"
}
$ApiPort = 8001
$DashboardPort = 8501
foreach ($Line in Get-Content -LiteralPath $EnvFile) {
    if ($Line -match '^\s*WORKFLOW_API_PORT\s*=\s*(\d+)\s*$') { $ApiPort = [int]$Matches[1] }
    if ($Line -match '^\s*WORKFLOW_DASHBOARD_PORT\s*=\s*(\d+)\s*$') { $DashboardPort = [int]$Matches[1] }
}
if ($ApiPort -lt 1 -or $ApiPort -gt 65535 -or $DashboardPort -lt 1 -or $DashboardPort -gt 65535) {
    throw "WORKFLOW_API_PORT 和 WORKFLOW_DASHBOARD_PORT 必须是 1 到 65535 之间的端口。"
}

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        & $PyLauncher.Source -3.11 -m venv (Join-Path $ProjectRoot ".venv")
    } else {
        $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $PythonCommand) { throw "未找到 Python 3.11+；请先安装 Python 并加入 PATH。" }
        $Version = & $PythonCommand.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ([version]$Version -lt [version]"3.11") { throw "需要 Python 3.11+，当前版本为 $Version。" }
        & $PythonCommand.Source -m venv (Join-Path $ProjectRoot ".venv")
    }
    if ($LASTEXITCODE -ne 0) { throw "创建 Python 虚拟环境失败。" }
}

& $VenvPython -m pip install -r (Join-Path $ProjectRoot "workflow\requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "安装 Python 依赖失败。" }

$Logs = Join-Path $ProjectRoot "workflow\logs"
New-Item -ItemType Directory -Path $Logs -Force | Out-Null
$PidFile = Join-Path $Logs "local-dev-processes.json"
if (Test-Path -LiteralPath $PidFile) {
    try {
        $Previous = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
        $Processes = @($Previous.api_pid, $Previous.dashboard_pid) | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }
        if ($Processes.Count -eq 2) {
            Write-Host "本地服务已在运行：API http://127.0.0.1:$ApiPort · 界面 http://127.0.0.1:$DashboardPort"
            if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$DashboardPort" }
            return
        }
    } catch { }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

$ApiProcess = Start-Process -FilePath $VenvPython -ArgumentList @("-m", "uvicorn", "workflow.api:app", "--host", "127.0.0.1", "--port", "$ApiPort") `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $Logs "api.stdout.log") `
    -RedirectStandardError (Join-Path $Logs "api.stderr.log")

$ApiReady = $false
for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
    if ($ApiProcess.HasExited) { break }
    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 2
        if ($Health.status -eq "ok") { $ApiReady = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $ApiReady) {
    Stop-Process -Id $ApiProcess.Id -Force -ErrorAction SilentlyContinue
    throw "FastAPI 未能启动。请查看 workflow\logs\api.stderr.log。"
}

$env:WORKFLOW_API_URL = "http://127.0.0.1:$ApiPort"
$DashboardProcess = Start-Process -FilePath $VenvPython `
    -ArgumentList @("-m", "streamlit", "run", "workflow/dashboard.py", "--server.address=127.0.0.1", "--server.port=$DashboardPort", "--browser.gatherUsageStats=false") `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $Logs "dashboard.stdout.log") `
    -RedirectStandardError (Join-Path $Logs "dashboard.stderr.log")
Start-Sleep -Seconds 2
if ($DashboardProcess.HasExited) {
    Stop-Process -Id $ApiProcess.Id -Force -ErrorAction SilentlyContinue
    throw "Streamlit 未能启动。请查看 workflow\logs\dashboard.stderr.log。"
}

@{ api_pid = $ApiProcess.Id; dashboard_pid = $DashboardProcess.Id } |
    ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding utf8
Write-Host "演示服务已启动："
Write-Host "  界面  http://127.0.0.1:$DashboardPort"
Write-Host "  API   http://127.0.0.1:$ApiPort"
Write-Host "停止服务：.\stop.ps1"
Write-Host "日志目录：workflow\logs"
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$DashboardPort" }
