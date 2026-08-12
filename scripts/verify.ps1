param(
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"

function Resolve-PythonExecutable {
    param([string]$ExplicitPath)

    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($ExplicitPath) {
        $candidates.Add($ExplicitPath)
    }

    if ($env:CONDA_DEFAULT_ENV -and $env:CONDA_DEFAULT_ENV -ne "base") {
        if ($env:CONDA_PREFIX) {
            $candidates.Add(
                (Join-Path $env:CONDA_PREFIX "envs\$env:CONDA_DEFAULT_ENV\python.exe")
            )
            $condaRoot = Split-Path -Parent $env:CONDA_PREFIX
            $candidates.Add(
                (Join-Path $condaRoot "envs\$env:CONDA_DEFAULT_ENV\python.exe")
            )
        }
        if ($env:CONDA_EXE) {
            $condaRoot = Split-Path -Parent (Split-Path -Parent $env:CONDA_EXE)
            $candidates.Add(
                (Join-Path $condaRoot "envs\$env:CONDA_DEFAULT_ENV\python.exe")
            )
        }
    }

    if ($env:CONDA_PREFIX) {
        $candidates.Add((Join-Path $env:CONDA_PREFIX "python.exe"))
    }

    $pythonCommand = Get-Command "python" -CommandType Application -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $candidates.Add($pythonCommand.Source)
    }

    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        & $candidate -c (
            "import importlib.util, sys; " +
            "sys.exit(0 if importlib.util.find_spec('pytest') and " +
            "importlib.util.find_spec('ruff') else 1)"
        )
        if ($LASTEXITCODE -eq 0) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw (
        "Cannot find a Python environment containing pytest and ruff. " +
        "Activate env_agent and run .\scripts\verify.ps1, or pass " +
        "-PythonExecutable <env_agent\python.exe>."
    )
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Arguments -join ' ')"
    }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Resolve-PythonExecutable -ExplicitPath $PythonExecutable
$testTempRoot = Join-Path $projectRoot ".test-tmp"
$testTempDir = Join-Path $testTempRoot ([guid]::NewGuid().ToString("N"))
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
New-Item -ItemType Directory -Path $testTempDir -Force | Out-Null
$env:TEMP = $testTempDir
$env:TMP = $testTempDir
Push-Location $projectRoot
try {
    Write-Host "Python: $pythonExe"
    Write-Host "Test temp: $testTempDir"

    Write-Host "[1/5] Ruff check"
    Invoke-Checked -Command $pythonExe -Arguments @(
        "-m", "ruff", "check", "app", "spikes", "tests", "chainlit_app.py"
    )

    Write-Host "[2/5] Ruff format check"
    Invoke-Checked -Command $pythonExe -Arguments @(
        "-m", "ruff", "format", "--check", "app", "spikes", "tests", "chainlit_app.py"
    )

    Write-Host "[3/5] Pytest"
    Invoke-Checked -Command $pythonExe -Arguments @(
        "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", $testTempDir
    )

    Write-Host "[4/5] Dependency consistency"
    Invoke-Checked -Command $pythonExe -Arguments @("-m", "pip", "check")

    Write-Host "[5/5] Independent MCP stdio smoke"
    Invoke-Checked -Command $pythonExe -Arguments @("-m", "tests.stdio_research_smoke")

    Write-Host "Credra Agent verification passed."
}
finally {
    Pop-Location
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    $resolvedProjectRoot = (Resolve-Path -LiteralPath $projectRoot).Path
    $resolvedTestTempRoot = (Resolve-Path -LiteralPath $testTempRoot).Path
    if ($resolvedTestTempRoot.StartsWith(
        $resolvedProjectRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        Remove-Item -LiteralPath $testTempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
