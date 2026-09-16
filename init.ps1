param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppPilotArgs
)

$Directory = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Directory ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Error "AppPilot setup is incomplete. Run .\setup.cmd first."
    exit 1
}

& $Python (Join-Path $Directory "src\apppilot_init.py") @AppPilotArgs
exit $LASTEXITCODE
