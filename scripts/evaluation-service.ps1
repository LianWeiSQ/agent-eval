param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765,
    [string]$DataDirectory = "",
    [switch]$WithSupervisor,
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
if (-not $DataDirectory) {
    $ExistingDataDirectory = Join-Path $ProjectRoot ".data-supervised-loop"
    if (Test-Path -LiteralPath (Join-Path $ExistingDataDirectory "evaluation.db")) {
        $DataDirectory = $ExistingDataDirectory
    }
    else {
        $DataDirectory = Join-Path $ProjectRoot ".data"
    }
}
$DataDirectory = [IO.Path]::GetFullPath($DataDirectory)
$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    $PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        throw "Python was not found. Install Python 3.12+ and activate your virtual environment."
    }
    $Python = $PythonPath
}

if ($WithSupervisor -and [string]::IsNullOrWhiteSpace($env:EVAL_SUPERVISOR_API_KEY)) {
    $SupervisorSecret = Read-Host "Supervisor API Key (hidden input; stored only for this process)" -AsSecureString
    try {
        $env:EVAL_SUPERVISOR_API_KEY = ([System.Net.NetworkCredential]::new("", $SupervisorSecret)).Password.Trim()
    }
    finally {
        $SupervisorSecret.Dispose()
    }
    if ([string]::IsNullOrWhiteSpace($env:EVAL_SUPERVISOR_API_KEY)) {
        throw "No key entered. Existing service was not stopped."
    }
}
elseif ($WithSupervisor) {
    Write-Host "Supervisor key already set in this terminal; reusing it (no input prompt needed)."
}

$Listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($Listeners.Count -gt 0) {
    if (-not $Restart) {
        throw "Port $Port is already in use. Reuse the running page, or use -WithSupervisor -Restart to enable supervision."
    }
    if ($HostAddress -notin @("127.0.0.1", "localhost")) {
        throw "Automatic restart only supports localhost."
    }
    $DataArgument = '--data-dir\s+(?:"' + [regex]::Escape($DataDirectory) + '"|' + [regex]::Escape($DataDirectory) + ')(?:\s|$)'
    $RestartTargets = @()
    foreach ($ListenerId in @($Listeners.OwningProcess | Select-Object -Unique)) {
        $ExistingService = Get-CimInstance Win32_Process -Filter "ProcessId=$ListenerId"
        if ($ExistingService.Name -notmatch '^python(?:\d+(?:\.\d+)?)?\.exe$' -or
            $ExistingService.CommandLine -notmatch '-m\s+agent_eval\s+serve(?:\s|$)' -or
            $ExistingService.CommandLine -notmatch $DataArgument) {
            throw "Port $Port belongs to another process or data directory. Nothing was stopped."
        }
        $RestartTargets += $ListenerId
    }
    $ExistingJobs = (Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/jobs" -TimeoutSec 10).data
    $ActiveJobs = @($ExistingJobs | Where-Object { $_.status -in @("queued", "running", "grading", "canceling") })
    if ($ActiveJobs.Count -gt 0) {
        throw "Evaluations are still running. Wait for completion before restarting. Nothing was stopped."
    }
    foreach ($ListenerId in $RestartTargets) {
        Stop-Process -Id $ListenerId -Force
    }
    Write-Host "Restarting Eval with the same history database."
}
$Arguments = @("-m", "agent_eval", "serve", "--host", $HostAddress, "--port", $Port)
if ($DataDirectory) {
    $Arguments += @("--data-dir", $DataDirectory)
}
Push-Location $ProjectRoot
try {
    & $Python @Arguments
}
finally {
    Pop-Location
}
