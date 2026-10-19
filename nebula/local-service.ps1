param([ValidateSet('start','stop','status','refresh')][string]$Action = 'start')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$serviceUrl = 'http://127.0.0.1:8768'
$pidFile = Join-Path $PSScriptRoot 'data/service.pid'
$pythonExe = (Get-Command python.exe).Source
if ($Action -eq 'refresh') {
    & $pythonExe -X utf8 adapters/local_experience.py --import-db data/nebula.sqlite
    if ($LASTEXITCODE -ne 0) { throw 'Import failed; check the reported source data error.' }
    exit
}
if ($Action -eq 'stop') {
    if (Test-Path -LiteralPath $pidFile) {
        $serviceProcessId = [int](Get-Content -LiteralPath $pidFile)
        $serviceProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$serviceProcessId"
        if ($serviceProcess -and $serviceProcess.CommandLine -like '*-m nebula*serve --port 8768*' -and $serviceProcess.CommandLine.Contains($PSScriptRoot)) {
            Stop-Process -Id $serviceProcessId
            Remove-Item -LiteralPath $pidFile
            Write-Output 'Knowledge Nebula stopped.'
        } elseif ($serviceProcess) { throw 'PID belongs to another process; leaving it running.' }
    }
    exit
}
try { $serviceHealth = Invoke-RestMethod "$serviceUrl/api/health" -TimeoutSec 3 } catch { $serviceHealth = $null }
if ($serviceHealth.app -eq 'knowledge-nebula') { Write-Output "Running: $serviceUrl/"; exit }
if ($Action -eq 'status') { Write-Output 'Not running.'; exit }
if (-not (Test-Path -LiteralPath 'data/nebula.sqlite')) { throw 'Run refresh first to import local experience.' }
$serviceProcess = Start-Process -FilePath $pythonExe -WorkingDirectory $PSScriptRoot -ArgumentList @('-X','utf8','-u','-m','nebula','--db',('"{0}"' -f (Join-Path $PSScriptRoot 'data/nebula.sqlite')),'serve','--port','8768') -WindowStyle Hidden -PassThru -RedirectStandardOutput 'data/service.stdout.log' -RedirectStandardError 'data/service.stderr.log'
$serviceProcess.Id | Set-Content -LiteralPath $pidFile
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 300
    try {
        $serviceHealth = Invoke-RestMethod "$serviceUrl/api/health" -TimeoutSec 2
        if ($serviceHealth.app -eq 'knowledge-nebula') { Write-Output "Started: $serviceUrl/"; exit }
    } catch {}
    if ($serviceProcess.HasExited) { break }
}
throw 'Startup failed. Read data/service.stderr.log.'
