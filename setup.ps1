param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppPilotArgs
)

$Directory = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-SupportedPython {
    param(
        [string]$Executable,
        [string[]]$Prefix = @()
    )

    & $Executable @Prefix -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)"
    return $LASTEXITCODE -eq 0
}

function Start-AppPilotSetup {
    param(
        [string]$Executable,
        [string[]]$Prefix = @()
    )

    & $Executable @Prefix (Join-Path $Directory "src\apppilot_bootstrap.py") @AppPilotArgs
    exit $LASTEXITCODE
}

$Candidates = @(
    $(if ($env:VIRTUAL_ENV) { Join-Path $env:VIRTUAL_ENV "Scripts\python.exe" }),
    (Join-Path $Directory ".venv\Scripts\python.exe")
)

foreach ($Candidate in $Candidates) {
    if ($Candidate -and (Test-Path $Candidate) -and (Test-SupportedPython $Candidate)) {
        Start-AppPilotSetup $Candidate
    }
}

$PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($PythonCommand -and (Test-SupportedPython $PythonCommand.Source)) {
    Start-AppPilotSetup $PythonCommand.Source
}

$PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
foreach ($Version in @("-3.13", "-3.12", "-3.11")) {
    if ($PyLauncher -and (Test-SupportedPython $PyLauncher.Source @($Version))) {
        Start-AppPilotSetup $PyLauncher.Source @($Version)
    }
}

$Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
if ($Winget) {
    $Answer = Read-Host "AppPilot requires Python 3.11-3.13. Install Python 3.13 with WinGet now? [y/N]"
    if ($Answer -match "^(y|yes)$") {
        & $Winget.Source install --id Python.Python.3.13 --exact --accept-source-agreements --accept-package-agreements
        Write-Host "Python installation completed. Open a new PowerShell window and run .\setup.cmd again."
        exit $LASTEXITCODE
    }
}

Write-Error "AppPilot requires Python 3.11-3.13. Install a supported Python version, then run .\setup.cmd again."
exit 1
