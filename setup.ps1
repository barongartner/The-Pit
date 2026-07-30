# The Pit -- Windows setup.
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Or, from nothing at all, in PowerShell:
#
#   git clone https://github.com/barongartner/The-Pit.git; cd The-Pit; powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Installs uv if missing, syncs dependencies, checks the claude CLI, and prints
# the two commands to run. Safe to re-run.

$ErrorActionPreference = "Stop"

function Ok($m)   { Write-Host "  OK    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  WARN  $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "  FAIL  $m" -ForegroundColor Red }

Write-Host ""
Write-Host "The Pit -- setup" -ForegroundColor Cyan
Write-Host ""

# --- git -------------------------------------------------------------------
if (Get-Command git -ErrorAction SilentlyContinue) {
    Ok "git found"
} else {
    Bad "git not found. Install it: winget install --id Git.Git"
    exit 1
}

# --- git hooks -------------------------------------------------------------
# .git/hooks is not tracked, so the hooks live in .githooks and git is pointed
# at them. Without this the post-merge reminder never fires on this machine.
git config core.hooksPath .githooks
Ok "git hooks enabled (post-merge reminder)"

# --- uv --------------------------------------------------------------------
# Manages Python itself, so there is no separate Python install step and the
# system Python is left alone.
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Ok "uv found ($(uv --version))"
} else {
    Warn "uv not found, installing"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Ok "uv installed"
    } else {
        Bad "uv install failed. Open a new terminal and re-run this script."
        exit 1
    }
}

# --- dependencies ----------------------------------------------------------
Write-Host ""
Write-Host "Syncing dependencies (uv fetches Python 3.12 on first run)..."
uv sync
Ok "dependencies ready"

Write-Host ""
Write-Host "Running tests..."
uv run pytest -q
Ok "tests pass"

# --- contact email ---------------------------------------------------------
# SEC EDGAR requires a contact address in the User-Agent and returns a bare 403
# without one. Persisted to the user environment so new terminals inherit it.
Write-Host ""
if ($env:THEPIT_CONTACT_EMAIL) {
    Ok "THEPIT_CONTACT_EMAIL is set"
} else {
    $email = Read-Host "Contact email for SEC requests (they require one)"
    if ($email) {
        [Environment]::SetEnvironmentVariable("THEPIT_CONTACT_EMAIL", $email, "User")
        $env:THEPIT_CONTACT_EMAIL = $email
        Ok "THEPIT_CONTACT_EMAIL saved for future terminals"
    } else {
        Warn "skipped -- the SEC filings feed will 403 until this is set"
    }
}

# --- claude CLI ------------------------------------------------------------
# The agent shells out to this. Atlas already uses it on this machine, so it is
# usually present and logged in already.
Write-Host ""
if (Get-Command claude -ErrorAction SilentlyContinue) {
    Ok "claude CLI found"
    Warn "if sessions report 'not logged in', run: claude   then /login"
} else {
    Warn "claude CLI not on PATH."
    Warn "Sessions will fall back to the deterministic baseline, which still trades."
    Warn "For real Claude sessions, install Claude Code and make sure `claude` runs."
}

# --- done ------------------------------------------------------------------
Write-Host ""
Write-Host "Ready. Two processes, two terminals:" -ForegroundColor Cyan
Write-Host ""
Write-Host "  uv run python -m thepit.engine.main     # records prices and filings"
Write-Host "  uv run python -m thepit.api.main       # dashboard on http://localhost:8000"
Write-Host ""
Write-Host "Then open http://localhost:8000 and press MFT Session."
Write-Host ""
Write-Host "Other useful commands:" -ForegroundColor Cyan
Write-Host "  uv run tradectl status      # is the engine alive"
Write-Host "  uv run tradectl sessions    # scoreboard with correct P&L"
Write-Host "  uv run tradectl uptime      # feed reliability"
Write-Host ""
Write-Host "Emergency stop, works even if everything else is wedged:"
Write-Host "  New-Item -ItemType File -Force `$env:USERPROFILE\.thepit\state\KILL"
Write-Host ""
