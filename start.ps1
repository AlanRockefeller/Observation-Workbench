$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $pythonCommand) {
    Write-Error "Could not find Python. Activate .venv-win before running this script."
    exit 1
}

$python = $pythonCommand.Source
# Avoid nested quotes here: PowerShell strips its own quoting before passing
# the command string to a native executable.
$pythonVersion = & $python -c 'import sys; print(chr(46).join(map(str, sys.version_info[:3])))'
if ($LASTEXITCODE -ne 0) {
    Write-Error "Could not determine the Python version for: $python"
    exit 1
}

& $python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python 3.11+ is required; found $pythonVersion at: $python"
    exit 1
}

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $python -c 'import PySide6' 2>$null
$pysideExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($pysideExitCode -ne 0) {
    Write-Error "PySide6 is not installed for: $python. Install project dependencies with: python -m pip install -r requirements.txt"
    exit 1
}

# Forward application arguments while handling the launcher-only scale options.
$appArgs = [System.Collections.Generic.List[string]]::new()
$scale = $env:SCALE
$scaleExplicit = $false

for ($index = 0; $index -lt $args.Count; $index++) {
    $argument = $args[$index]
    if ($argument -like "--scale=*") {
        $scale = $argument.Substring("--scale=".Length)
        $scaleExplicit = $true
    }
    elseif ($argument -eq "-s") {
        if ($index + 1 -lt $args.Count) {
            $index++
            $scale = $args[$index]
        }
        $scaleExplicit = $true
    }
    else {
        $appArgs.Add($argument)
    }
}

if ($scaleExplicit -and -not [string]::IsNullOrWhiteSpace($scale)) {
    $env:QT_SCALE_FACTOR = $scale
}

# Note: unlike start.sh, this does not pin QT_FONT_DPI. That pin works around
# WSLg re-negotiating the virtual display's DPI on suspend/resume; native
# Windows reports stable DPI and already scales fonts correctly on its own.
# Forcing QT_FONT_DPI=96 here would override that scaling down to a 100%
# baseline, producing tiny text on any display above 100% OS scaling (e.g.
# the default 150-200% on a 4K panel). Set SCALE or --scale to adjust overall
# UI scaling, or use the in-app UI scale setting.

& $python main.py @appArgs
exit $LASTEXITCODE
