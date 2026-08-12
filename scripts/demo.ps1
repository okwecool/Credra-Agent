param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("normal", "risky", "retry", "status", "approve", "research")]
    [string]$Scenario,
    [string]$ThreadId,
    [string]$Comment = "Credra Agent Day6 demo review.",
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
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "Cannot find Python. Pass -PythonExecutable <env_agent\python.exe>."
}

function Invoke-TaskCli {
    param([string[]]$Arguments)

    & $script:pythonExe -m app.task_cli @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Task CLI failed with exit code $LASTEXITCODE."
    }
}

if (-not $ThreadId) {
    if ($Scenario -in @("status", "approve", "research")) {
        throw "-$Scenario requires -ThreadId."
    }
    $ThreadId = "showcase-$Scenario-$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())"
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Resolve-PythonExecutable -ExplicitPath $PythonExecutable
$dataDir = if ($env:DATA_DIR) { $env:DATA_DIR } else { "data" }
$traceDir = if ($env:TRACE_DIR) { $env:TRACE_DIR } else { "traces" }
Push-Location $projectRoot
try {
    Write-Host "Python: $pythonExe"

    switch ($Scenario) {
        "normal" {
            Invoke-TaskCli -Arguments @(
                "start", "--thread-id", $ThreadId, "--case-id", "case_normal"
            )
            Write-Host "Report: $dataDir/case_normal/output/credit_report.md"
        }
        "risky" {
            Invoke-TaskCli -Arguments @(
                "start", "--thread-id", $ThreadId, "--case-id", "case_risky"
            )
            Write-Host "Resume in another process:"
            Write-Host "powershell -File scripts/demo.ps1 approve -ThreadId $ThreadId"
        }
        "retry" {
            $previousFailFirst = $env:RESEARCH_FAIL_FIRST
            $previousMaxRetry = $env:MAX_RETRY
            try {
                $env:RESEARCH_FAIL_FIRST = "1"
                $env:MAX_RETRY = "2"
                Invoke-TaskCli -Arguments @(
                    "start", "--thread-id", $ThreadId, "--case-id", "case_risky"
                )
                Write-Host "Trace: $traceDir/$ThreadId.jsonl"
            }
            finally {
                $env:RESEARCH_FAIL_FIRST = $previousFailFirst
                $env:MAX_RETRY = $previousMaxRetry
            }
        }
        "status" {
            Invoke-TaskCli -Arguments @("status", "--thread-id", $ThreadId)
        }
        "approve" {
            Invoke-TaskCli -Arguments @(
                "resume", "--thread-id", $ThreadId, "--decision", "approve", "--comment", $Comment
            )
        }
        "research" {
            Invoke-TaskCli -Arguments @(
                "resume", "--thread-id", $ThreadId, "--decision", "research", "--comment", $Comment
            )
        }
    }

    Write-Host "Thread ID: $ThreadId"
}
finally {
    Pop-Location
}
