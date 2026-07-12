<#
.SYNOPSIS
    Windows equivalent of the Makefile. Same target names.
.EXAMPLE
    ./make.ps1 check      # lint + typecheck + test (the phase gate)
    ./make.ps1 up         # docker compose up
    ./make.ps1 seed
#>
param(
    [Parameter(Position = 0)]
    [string]$Target = "help",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Bin = Join-Path $Root ".venv\Scripts"
$Py = Join-Path $Bin "python.exe"

function Invoke-Step {
    param([string]$Exe, [string[]]$Args)
    $path = Join-Path $Bin $Exe
    if (-not (Test-Path $path)) { $path = $Exe }
    & $path @Args
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}

switch ($Target) {
    "help" {
        Write-Host "DANAH targets:" -ForegroundColor Cyan
        @(
            "  venv        Create the Python 3.12 virtualenv",
            "  install     Install runtime + dev dependencies",
            "  dev         Run the API with autoreload",
            "  worker      Run the ARQ worker",
            "  scheduler   Run the ARQ cron scheduler",
            "  up/down     Start/stop the Docker stack",
            "  lint        ruff check + format check",
            "  format      ruff auto-fix",
            "  typecheck   mypy --strict app",
            "  test        pytest",
            "  test-cov    pytest with coverage",
            "  check       lint + typecheck + test (phase gate)",
            "  migrate     alembic upgrade head",
            "  seed        seed database",
            "  smoke       live acceptance check (needs API keys)",
            "  loadtest    async burst load test"
        ) | ForEach-Object { Write-Host $_ }
    }
    "venv" { & uv venv --python 3.12 (Join-Path $Root ".venv") }
    "install" { & uv pip install --python $Py -e ".[dev]" }

    "dev" { Invoke-Step "uvicorn.exe" @("app.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000") }
    "worker" { Invoke-Step "arq.exe" @("app.workers.worker.WorkerSettings") }
    "scheduler" { Invoke-Step "arq.exe" @("app.workers.worker.SchedulerSettings") }

    "up" { & docker compose up -d --build }
    "down" { & docker compose down }
    "logs" { & docker compose logs -f api }

    "lint" {
        Invoke-Step "ruff.exe" @("check", "app", "tests", "scripts")
        Invoke-Step "ruff.exe" @("format", "--check", "app", "tests", "scripts")
    }
    "format" {
        Invoke-Step "ruff.exe" @("check", "--fix", "app", "tests", "scripts")
        Invoke-Step "ruff.exe" @("format", "app", "tests", "scripts")
    }
    "typecheck" { Invoke-Step "mypy.exe" @("--strict", "app") }
    "test" { Invoke-Step "pytest.exe" $Rest }
    "test-cov" { Invoke-Step "pytest.exe" @("--cov=app", "--cov-report=term-missing", "--cov-report=html") }
    "check" {
        Invoke-Step "ruff.exe" @("check", "app", "tests", "scripts")
        Invoke-Step "ruff.exe" @("format", "--check", "app", "tests", "scripts")
        Invoke-Step "mypy.exe" @("--strict", "app")
        Invoke-Step "pytest.exe" @()
        Write-Host "`nAll gates green." -ForegroundColor Green
    }

    "migrate" { Invoke-Step "alembic.exe" @("upgrade", "head") }
    "migration" { Invoke-Step "alembic.exe" (@("revision", "--autogenerate", "-m") + $Rest) }
    "downgrade" { Invoke-Step "alembic.exe" @("downgrade", "-1") }
    "seed" { Invoke-Step "python.exe" @("-m", "scripts.seed") }
    "smoke" { Invoke-Step "python.exe" @("-m", "scripts.smoke_test") }
    "loadtest" { Invoke-Step "python.exe" @("-m", "scripts.loadtest") }

    default { Write-Error "Unknown target '$Target'. Run ./make.ps1 help" }
}
