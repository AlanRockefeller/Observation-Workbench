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

# Keep text sizing stable across display reconnects. Set SCALE or --scale to
# adjust overall UI scaling, or use the in-app UI scale setting.
if ([string]::IsNullOrWhiteSpace($env:QT_FONT_DPI)) {
    $env:QT_FONT_DPI = "96"
}

& $python main.py @appArgs
exit $LASTEXITCODE
