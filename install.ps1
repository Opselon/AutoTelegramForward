<#
.SYNOPSIS
  AutoTelegramForward — one-command installer (Windows).

.DESCRIPTION
  Install:
      irm https://raw.githubusercontent.com/Opselon/AutoTelegramForward/master/install.ps1 | iex

  What this does:
    1. Makes sure git and Python >= 3.10 (with pip + venv) are available,
       installing them with winget if they are missing.
    2. Clones AutoTelegramForward (unless you are already inside a copy).
    3. Hands off to:   python atf.py setup
       which asks ONLY for your Telegram bot token and does the rest:
       virtualenv, dependencies, config.yaml, and an auto-generated
       encryption master key.

.PARAMETER Dir
  Directory to clone into (env: ATF_INSTALL_DIR).

.PARAMETER Branch
  Branch/tag to clone (env: ATF_BRANCH). Default: master.

.PARAMETER WithGo
  Also install the Go toolchain (optional REST API).

.PARAMETER Force
  Replace an existing clone.
#>
[CmdletBinding()]
param(
  [string]$Dir = $env:ATF_INSTALL_DIR,
  [string]$Branch = $(if ($env:ATF_BRANCH) { $env:ATF_BRANCH } else { "master" }),
  [switch]$WithGo,
  [switch]$Force
)

$ErrorActionPreference = "Stop"

$Repo     = "Opselon/AutoTelegramForward"
$RepoUrl  = "https://github.com/$Repo.git"

function Write-Step { param([string]$Msg) Write-Host "  [$Msg]" -ForegroundColor Green }
function Write-Info { param([string]$Msg) Write-Host "  $Msg" -ForegroundColor DarkGray }
function Fail      { param([string]$Msg) Write-Host "  ✗ $Msg" -ForegroundColor Red; exit 1 }

function Have-Command {
  param([string]$Name)
  return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Ensure-Winget {
  if (Have-Command "winget") { return $true }
  Write-Info "winget not found — skipping auto-install; please install missing tools manually."
  return $false
}

# ---------------------------------------------------------------- banner ----
Write-Host ""
Write-Host "    ___                _____         _            _____           _ " -ForegroundColor Cyan
Write-Host "   / _ |  ___  ___ _ / ___/  ____  (_)__  ___   / ___/  ____ __ (_)__ ___" -ForegroundColor Cyan
Write-Host "  / __ | / _ \/ _  // /__   / __ \/ / _ \(_-<  / /__   / __// // / -_) _ \" -ForegroundColor Cyan
Write-Host " /_/ |_|/ ./\_,_/ \___/  /_/ /_/_/\___/___/  \___/  /_/   \_,_/\__/_//_/" -ForegroundColor Cyan
Write-Host "        /_/    Telegram auto-forwarder  •  bot + Go REST API" -ForegroundColor Cyan
Write-Host ""
Write-Host "  One-command installer — you only need a Telegram bot token (from @BotFather)." -ForegroundColor DarkGray
Write-Host ""

# ------------------------------------------------------- 1. prerequisites ---
Write-Step "1/4  Checking prerequisites ..."

$hasWinget = Ensure-Winget

if (-not (Have-Command "git")) {
  if ($hasWinget) {
    Write-Info "Installing git via winget ..."
    winget install --silent --accept-source-agreements --accept-package-agreements Git.Git | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
  }
  if (-not (Have-Command "git")) { Fail "git is required. Install it from https://git-scm.com and re-run." }
}
Write-Info "git: $(git --version)"

$py = $null
foreach ($cand in @("python", "py")) {
  try {
    $v = & $cand --version 2>&1
    if ($v -match "Python\s+(\d+)\.(\d+)") {
      if ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10)) {
        $py = $cand
        Write-Info "Python: $v"
        break
      }
    }
  } catch { }
}
if (-not $py) {
  if ($hasWinget) {
    Write-Info "Installing Python 3.12 via winget ..."
    winget install --silent --accept-source-agreements --accept-package-agreements Python.Python.3.12 | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    if (Have-Command "python") { $py = "python" }
  }
  if (-not $py) { Fail "Python >= 3.10 is required. Install it from https://www.python.org and re-run." }
}

if ($WithGo -and -not (Have-Command "go")) {
  if ($hasWinget) {
    Write-Info "Installing Go via winget (optional REST API) ..."
    winget install --silent --accept-source-agreements --accept-package-agreements GoLang.Go | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
  }
  if (-not (Have-Command "go")) { Write-Info "Go not installed — REST API will be skipped (bot still works 100%)." }
}

# ------------------------------------------------------------- 2. clone -----
Write-Step "2/4  Getting the code ..."

$inRepo = $false
try {
  git rev-parse --show-toplevel 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { $inRepo = $true }
} catch { }

if ($inRepo -and -not $Dir) {
  $target = (git rev-parse --show-toplevel).Trim()
  Write-Info "Already inside a git checkout: $target"
} else {
  if (-not $Dir) { $Dir = Join-Path $HOME "AutoTelegramForward" }
  if ((Test-Path $Dir) -and $Force) {
    Write-Info "Removing existing directory: $Dir"
    Remove-Item -Recurse -Force $Dir
  }
  if (-not (Test-Path $Dir)) {
    Write-Info "Cloning $RepoUrl (branch $Branch) ..."
    git clone --branch $Branch --depth 1 $RepoUrl $Dir
  } else {
    Write-Info "Using existing directory: $Dir"
  }
  $target = $Dir
}

Set-Location $target

# ---------------------------------------------------------- 3. preflight ----
Write-Step "3/4  Preflight ..."
try {
  & $py -c "import venv" 2>$null
} catch {
  Fail "Python venv module is missing — reinstall Python with default options."
}
Write-Info "Ready: $(git log -1 --format='%h %s' 2>$null)"

# ------------------------------------------------------------- 4. setup -----
Write-Step "4/4  Setup (asks only for your bot token) ..."
Write-Host ""
& $py atf.py setup
