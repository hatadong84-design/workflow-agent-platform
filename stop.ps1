$ErrorActionPreference = "Stop"
$PidFile = Join-Path $PSScriptRoot "workflow\logs\local-dev-processes.json"
if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host "没有找到本地演示服务的 PID 文件。"
    exit 0
}

$Processes = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
foreach ($ProcessId in @($Processes.dashboard_pid, $Processes.api_pid)) {
    if ($ProcessId) { Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue }
}
Remove-Item -LiteralPath $PidFile -Force
Write-Host "本地 FastAPI 和 Streamlit 服务已停止。"
