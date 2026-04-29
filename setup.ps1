<#
.SYNOPSIS
    First-time setup for wisper-transcribe on Windows with CUDA support.
.DESCRIPTION
    Creates a virtual environment, installs PyTorch (CUDA or CPU build),
    then installs the package so that all ML dependencies resolve against
    the correct torch variant from the start.
.EXAMPLE
    .\setup.ps1
#>

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK  : $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "   FAIL: $msg" -ForegroundColor Red; exit 1 }

# Runs a pip command with a Write-Progress bar so the user can see the install is running.
# Captures stdout/stderr; on failure prints the captured output then exits.
function Invoke-PipWithProgress {
    param(
        [string]$Activity,
        [string[]]$PipArgs
    )
    $tmpOut = [System.IO.Path]::GetTempFileName()
    $tmpErr = [System.IO.Path]::GetTempFileName()
    $resolvedPip = (Resolve-Path $pip).Path
    $proc = Start-Process -FilePath $resolvedPip `
        -ArgumentList $PipArgs `
        -RedirectStandardOutput $tmpOut `
        -RedirectStandardError  $tmpErr `
        -PassThru -NoNewWindow
    $i = 0
    while (-not $proc.HasExited) {
        # Crawl 0.5 ppt/s → reaches 90 % in ~3 min, then holds until done
        $pct = [Math]::Min(90, [Math]::Floor($i * 0.5))
        Write-Progress -Activity $Activity -Status "This may take several minutes..." -PercentComplete $pct
        $i++
        Start-Sleep -Milliseconds 1000
    }
    Write-Progress -Activity $Activity -Completed
    if ($proc.ExitCode -ne 0) {
        $out = Get-Content $tmpOut -Raw -ErrorAction SilentlyContinue
        $err = Get-Content $tmpErr -Raw -ErrorAction SilentlyContinue
        if ($out) { Write-Host $out }
        if ($err) { Write-Host $err -ForegroundColor Red }
        Remove-Item $tmpOut, $tmpErr -ErrorAction SilentlyContinue
        Write-Fail "$Activity failed (exit code $($proc.ExitCode))"
    }
    Remove-Item $tmpOut, $tmpErr -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "wisper-transcribe setup (Windows)" -ForegroundColor White
Write-Host "===================================" -ForegroundColor White

# ── Python ───────────────────────────────────────────────────────────────────
Write-Step "Checking Python..."
try {
    $ver = & python -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro}')" 2>&1
    $parts = $ver.Trim().Split('.')
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 10)) {
        Write-Fail "Python $ver found but 3.10+ is required. Install from https://python.org"
    }
    Write-OK "Python $ver"
} catch {
    Write-Fail "Python not found. Install Python 3.10+ from https://python.org"
}

# ── Virtual environment ───────────────────────────────────────────────────────
Write-Step "Setting up virtual environment..."
if (-not (Test-Path ".venv")) {
    python -m venv .venv
    Write-OK "Created .venv"
} else {
    Write-OK ".venv already exists"
}

$pip    = ".\.venv\Scripts\pip.exe"
$python = ".\.venv\Scripts\python.exe"

# ── PyTorch installation ─────────────────────────────────────────────────────
# Install PyTorch BEFORE the package. If torch is installed after, pip pulls
# the CPU-only PyPI build as a transitive dependency first, and packages like
# faster-whisper and torchaudio bind to that build. A subsequent force-reinstall
# of the CUDA wheels replaces torch itself but leaves those packages referencing
# stale internals — causing "module 'torch' has no attribute '_utils'" at runtime.
# Installing the correct build first means pip reuses it for all dependents.
$hasNvidia = & nvidia-smi --list-gpus 2>$null
if ($null -eq $hasNvidia) { $hasNvidia = $false }

if ($hasNvidia) {
    Write-Step "NVIDIA GPU detected — installing PyTorch with CUDA 12.6 first (~2 GB)..."
    Write-Host "   (PyPI torch is CPU-only; installing the GPU build before the package)" -ForegroundColor Gray
    Invoke-PipWithProgress "Downloading PyTorch + CUDA 12.6 (~2 GB)" @(
        "install", "torch>=2.8.0", "torchaudio>=2.8.0",
        "--index-url", "https://download.pytorch.org/whl/cu126", "-q"
    )
} else {
    Write-Step "No NVIDIA GPU detected — installing PyTorch (CPU build)..."
    Invoke-PipWithProgress "Installing PyTorch (CPU build)" @("install", "torch>=2.8.0", "torchaudio>=2.8.0", "-q")
}

# ── Install package ───────────────────────────────────────────────────────────
# torch is already present; pip will not pull the CPU fallback from PyPI.
Write-Step "Installing wisper-transcribe (this may take several minutes)..."
Invoke-PipWithProgress "Installing wisper-transcribe" @("install", "-e", ".", "-q")
Write-OK "wisper-transcribe installed"

$cudaAvailable = & $python -c "import torch; print(torch.cuda.is_available())"
if ($cudaAvailable -eq "True") {
    $gpuName = & $python -c "import torch; print(torch.cuda.get_device_name(0))"
    Write-OK "CUDA available — GPU: $gpuName"
} else {
    Write-OK "Running on CPU"
}

# ── ffmpeg ────────────────────────────────────────────────────────────────────
# Gyan.FFmpeg.Shared (not Gyan.FFmpeg) is required.
# The plain Gyan.FFmpeg package is a static build — executables only, no DLLs.
# torchcodec (used by pyannote for audio I/O) needs the shared DLLs
# (avcodec-*.dll, avformat-*.dll, etc.) that only the Shared build provides.
Write-Step "Checking ffmpeg..."
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-OK "ffmpeg found"
} else {
    Write-Warn "ffmpeg not found — installing Gyan.FFmpeg.Shared via winget..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install Gyan.FFmpeg.Shared --silent --accept-package-agreements --accept-source-agreements
        Write-OK "ffmpeg installed — restart your terminal before using wisper"
    } else {
        Write-Warn "winget not available. Download the ffmpeg 'full-shared' build from https://www.gyan.dev/ffmpeg/builds/ and add its bin\ folder to your PATH."
    }
}

# ── LLM Post-processing setup ─────────────────────────────────────────────────
$wisper = ".\.venv\Scripts\wisper.exe"

Write-Step "LLM Post-processing setup (wisper refine / wisper summarize)"
Write-Host ""
Write-Host "   Vocabulary correction and campaign notes need an LLM provider." -ForegroundColor Gray
Write-Host "   Local providers (Ollama / LM Studio) need no API key." -ForegroundColor Gray
Write-Host ""

# Probe Ollama and LM Studio
$ollamaRunning = $false; $ollamaModels = @()
try {
    $r = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 2 -ErrorAction Stop
    $ollamaRunning = $true
    $ollamaModels  = @($r.models | ForEach-Object { $_.name })
} catch {}

$lmRunning = $false; $lmModels = @()
try {
    $r = Invoke-RestMethod -Uri "http://localhost:1234/v1/models" -TimeoutSec 2 -ErrorAction Stop
    $lmRunning = $true
    $lmModels  = @($r.data | ForEach-Object { $_.id })
} catch {}

$ollamaTag = if ($ollamaRunning) { "  [running — $($ollamaModels.Count) model(s) available]" } else { "  [not running]" }
$lmTag     = if ($lmRunning)     { "  [running — $($lmModels.Count) model(s) loaded]"        } else { "  [not running]" }

Write-Host "   LOCAL — no API key needed:"
Write-Host "     o) Ollama     — localhost:11434$ollamaTag"
Write-Host "     l) LM Studio  — localhost:1234$lmTag"
Write-Host ""
Write-Host "   CLOUD — requires API key:"
Write-Host "     a) Anthropic (Claude)"
Write-Host "     b) OpenAI (GPT)"
Write-Host "     c) Google (Gemini)"
Write-Host "     d) All three cloud SDKs"
Write-Host ""
Write-Host "     s) Skip — configure later with: wisper config llm"
Write-Host ""

# Let user pick a model from a list; returns model name or $null
function Invoke-ModelPicker($provider, $models) {
    if ($models.Count -eq 0) {
        Write-Warn "$provider is running but has no models — load one then run: wisper config llm"
        return $null
    }
    Write-Host ""
    Write-Host "   Available $provider models:" -ForegroundColor Gray
    for ($i = 0; $i -lt $models.Count; $i++) {
        $hint = if ($i -eq 0) { "  <- suggested" } else { "" }
        Write-Host "     $($i + 1)) $($models[$i])$hint"
    }
    Write-Host ""
    $pick = Read-Host "   Pick a number [1] or Enter to configure later"
    if ([string]::IsNullOrWhiteSpace($pick)) { $pick = "1" }
    if ($pick -match '^\d+$') {
        $idx = [int]$pick - 1
        if ($idx -ge 0 -and $idx -lt $models.Count) { return $models[$idx] }
    }
    return $null
}

$LLMChoice = Read-Host "   Choice [o/l/a/b/c/d/s]"
switch ($LLMChoice.ToLower()) {
    "o" {
        if ($ollamaRunning) {
            $model = Invoke-ModelPicker "Ollama" $ollamaModels
            if ($model) {
                & $wisper config set llm_provider ollama
                & $wisper config set llm_model $model
                Write-OK "Ollama configured — model: $model"
            } else {
                Write-OK "Ollama selected — run 'wisper config llm' to set a model"
            }
        } else {
            Write-Host ""
            Write-Host "   Ollama is not running. To use it:" -ForegroundColor Gray
            Write-Host "     1. Install from https://ollama.com" -ForegroundColor Gray
            Write-Host "     2. Pull a model: ollama pull llama3.2" -ForegroundColor Gray
            Write-Host "     3. Configure:    wisper config llm" -ForegroundColor Gray
            Write-OK "Skipped — configure later with 'wisper config llm'"
        }
    }
    "l" {
        if ($lmRunning) {
            $model = Invoke-ModelPicker "LM Studio" $lmModels
            if ($model) {
                & $wisper config set llm_provider lmstudio
                & $wisper config set llm_model $model
                Write-OK "LM Studio configured — model: $model"
            } else {
                Write-OK "LM Studio selected — run 'wisper config llm' to set a model"
            }
        } else {
            Write-Host ""
            Write-Host "   LM Studio is not running. To use it:" -ForegroundColor Gray
            Write-Host "     1. Install from https://lmstudio.ai" -ForegroundColor Gray
            Write-Host "     2. Download a model and start the local server" -ForegroundColor Gray
            Write-Host "     3. Configure: wisper config llm" -ForegroundColor Gray
            Write-OK "Skipped — configure later with 'wisper config llm'"
        }
    }
    "a" { Invoke-PipWithProgress "Installing Anthropic SDK"    @("install", "-e", ".[llm-anthropic]", "-q"); Write-OK "anthropic SDK installed" }
    "b" { Invoke-PipWithProgress "Installing OpenAI SDK"       @("install", "-e", ".[llm-openai]",    "-q"); Write-OK "openai SDK installed" }
    "c" { Invoke-PipWithProgress "Installing Google Genai SDK" @("install", "-e", ".[llm-google]",    "-q"); Write-OK "google-genai SDK installed" }
    "d" { Invoke-PipWithProgress "Installing all LLM SDKs"    @("install", "-e", ".[llm-all]",        "-q"); Write-OK "all LLM SDKs installed" }
    default { Write-OK "Skipped — configure later with 'wisper config llm'" }
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Activate venv:       .venv\Scripts\activate"
Write-Host "  2. Run setup wizard:    wisper setup   (HF token, model download, LLM config)"
Write-Host "  3. First session:       wisper transcribe session01.mp3 --enroll-speakers"
Write-Host "  4. LLM config (later):  wisper config llm"
Write-Host ""
