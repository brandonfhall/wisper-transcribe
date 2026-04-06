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
Write-Step "Installing wisper-transcribe..."
& $pip install -e . -q
Write-OK "wisper-transcribe installed"

# ── PyTorch with CUDA ─────────────────────────────────────────────────────────
# The default 'pip install torch' gets the CPU-only build from PyPI.
# We must explicitly point pip at PyTorch's CUDA index to get GPU support.
# pyannote-audio 4.x requires torch>=2.8.0, which is on the cu126 index.
Write-Step "Installing PyTorch with CUDA 12.6 support..."
Write-Host "   (default pip install gets CPU-only PyTorch — this installs the GPU build)" -ForegroundColor Gray
& $pip install "torch>=2.8.0" "torchaudio>=2.8.0" --index-url https://download.pytorch.org/whl/cu126 --force-reinstall -q

$cudaAvailable = & $python -c "import torch; print(torch.cuda.is_available())"
if ($cudaAvailable -eq "True") {
    $gpuName = & $python -c "import torch; print(torch.cuda.get_device_name(0))"
    Write-OK "CUDA available — GPU: $gpuName"
} else {
    Write-Warn "CUDA not detected after install."
    Write-Host "   Check that your NVIDIA drivers are up to date (https://www.nvidia.com/drivers)" -ForegroundColor Yellow
    Write-Host "   wisper will still work with --device cpu" -ForegroundColor Yellow
}

# ── ffmpeg ────────────────────────────────────────────────────────────────────
Write-Step "Checking ffmpeg..."
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-OK "ffmpeg found"
} else {
    Write-Warn "ffmpeg not found — installing via winget..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
        Write-OK "ffmpeg installed — restart your terminal before using wisper"
    } else {
        Write-Warn "winget not available. Download ffmpeg from https://ffmpeg.org/download.html and add it to your PATH."
    }
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Activate venv:     .venv\Scripts\activate"
Write-Host "  2. Run setup wizard:  wisper setup   (configures HF token + pre-downloads models)"
Write-Host "  3. First session:     wisper transcribe session01.mp3 --enroll-speakers"
Write-Host ""
