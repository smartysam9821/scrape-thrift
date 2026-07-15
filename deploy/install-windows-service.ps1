param(
    [string]$ServiceName = "ThriftBooksScraperConsole",
    [string]$DisplayName = "ThriftBooks Scraper Console",
    [string]$ProjectDir = "C:\scrape-thrift",
    [string]$HostAddress = "0.0.0.0",
    [int]$Port = 8000,
    [string]$NssmPath = "nssm.exe"
)

$ErrorActionPreference = "Stop"

$PythonPath = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonPath)) {
    throw "Python venv not found at $PythonPath"
}

$NssmCommand = Get-Command $NssmPath -ErrorAction SilentlyContinue
if (-not $NssmCommand) {
    throw "NSSM was not found. Install NSSM and make nssm.exe available in PATH, or pass -NssmPath C:\path\to\nssm.exe"
}

$Existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($Existing) {
    Stop-Service -Name $ServiceName -ErrorAction SilentlyContinue
    & $NssmCommand.Source remove $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 2
}

& $NssmCommand.Source install $ServiceName $PythonPath "-m uvicorn app:app --host $HostAddress --port $Port" | Out-Null
& $NssmCommand.Source set $ServiceName AppDirectory $ProjectDir | Out-Null
& $NssmCommand.Source set $ServiceName DisplayName $DisplayName | Out-Null
& $NssmCommand.Source set $ServiceName Description "Runs the ThriftBooks FastAPI scraper web console." | Out-Null
& $NssmCommand.Source set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $NssmCommand.Source set $ServiceName AppStdout (Join-Path $ProjectDir "results\windows-service.out.log") | Out-Null
& $NssmCommand.Source set $ServiceName AppStderr (Join-Path $ProjectDir "results\windows-service.err.log") | Out-Null
& $NssmCommand.Source set $ServiceName AppRotateFiles 1 | Out-Null
& $NssmCommand.Source set $ServiceName AppRotateOnline 1 | Out-Null
& $NssmCommand.Source set $ServiceName AppRotateBytes 10485760 | Out-Null
& $NssmCommand.Source set $ServiceName AppRestartDelay 5000 | Out-Null
Start-Service -Name $ServiceName

Write-Host "Installed and started $ServiceName"
Write-Host "Project: $ProjectDir"
Write-Host "URL: http://localhost:$Port"
