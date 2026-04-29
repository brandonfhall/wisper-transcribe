<#
.SYNOPSIS
    First-time setup for wisper-transcribe on Windows with CUDA support.
.DESCRIPTION
    Creates a virtual environment, installs the package, installs PyTorch with
    CUDA 12.4 support (required for GPU acceleration), and checks ffmpeg.
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

# ── Install package ───────────────────────────────────────────────────────────
Write-Step "Installing wisper-transcribe (this may take several minutes)..."
Invoke-PipWithProgress "Installing wisper-transcribe" @("install", "-e", ".", "-q")
Write-OK "wisper-transcribe installed"

# ── PyTorch installation ─────────────────────────────────────────────────────
# Check if a CUDA-capable GPU is available via nvidia-smi or torch
$hasNvidia = & nvidia-smi --list-gpus 2>$null
if ($null -eq $hasNvidia) { $hasNvidia = $false }

if ($hasNvidia) {
    Write-Step "NVIDIA GPU detected. Installing PyTorch with CUDA 12.6 support..."
    Write-Host "   (default pip install gets CPU-only PyTorch — this installs the GPU build, ~2 GB)" -ForegroundColor Gray
    Invoke-PipWithProgress "Downloading PyTorch + CUDA 12.6 (~2 GB)" @(
        "install", "torch>=2.8.0", "torchaudio>=2.8.0",
        "--index-url", "https://download.pytorch.org/whl/cu126",
        "--force-reinstall", "-q"
    )
} else {
    Write-Step "No NVIDIA GPU detected. Installing PyTorch (CPU build)..."
    Invoke-PipWithProgress "Installing PyTorch (CPU build)" @("install", "torch>=2.8.0", "torchaudio>=2.8.0", "-q")
}

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

# ── Optional cloud LLM extras ────────────────────────────────────────────────
Write-Step "Optional: cloud LLM extras (wisper refine / wisper summarize)"
Write-Host ""
Write-Host "   wisper refine and wisper summarize use an LLM to clean up transcripts" -ForegroundColor Gray
Write-Host "   and generate campaign notes. Ollama (local) works out of the box." -ForegroundColor Gray
Write-Host "   Install an extra only if you want to use a cloud provider." -ForegroundColor Gray
Write-Host ""
Write-Host "     a) Anthropic (Claude)"
Write-Host "     b) OpenAI (GPT)"
Write-Host "     c) Google (Gemini)"
Write-Host "     d) All three"
Write-Host "     s) Skip (use Ollama or configure later)"
Write-Host ""
$LLMChoice = Read-Host "   Choice [a/b/c/d/s]"
switch ($LLMChoice.ToLower()) {
    "a" { Invoke-PipWithProgress "Installing Anthropic SDK" @("install", "-e", ".[llm-anthropic]", "-q"); Write-OK "anthropic SDK installed" }
    "b" { Invoke-PipWithProgress "Installing OpenAI SDK"    @("install", "-e", ".[llm-openai]",    "-q"); Write-OK "openai SDK installed" }
    "c" { Invoke-PipWithProgress "Installing Google Genai SDK" @("install", "-e", ".[llm-google]", "-q"); Write-OK "google-genai SDK installed" }
    "d" { Invoke-PipWithProgress "Installing all LLM SDKs" @("install", "-e", ".[llm-all]",        "-q"); Write-OK "all LLM SDKs installed" }
    default { Write-OK "Skipped — use 'wisper config llm' to set up a provider at any time" }
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
