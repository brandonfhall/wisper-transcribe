# ─────────────────────────────────────────────────────────────────────────────
# wisper-transcribe Dockerfile
#
# Two build targets:
#   gpu  (default) — PyTorch cu126 wheels; requires NVIDIA driver + Container Toolkit on host
#   cpu            — CPU-only, lighter image
#
# PyTorch CUDA wheels bundle the CUDA runtime (libcudart, libcublas, libcudnn)
# so a NVIDIA base image is NOT required — only the host driver is needed for
# GPU passthrough via NVIDIA Container Toolkit.
#
# Build:
#   docker compose build                  # builds gpu target (default)
#   docker compose build wisper-cpu       # builds cpu target
#
# Run:
#   docker compose run wisper wisper setup
#   docker compose run wisper wisper transcribe /app/input/session.mp3 --enroll-speakers
# ─────────────────────────────────────────────────────────────────────────────

# ── Java sidecar builder ──────────────────────────────────────────────────────
# Builds the JDA + JDAVE Discord recording sidecar fat JAR (discord-bot/) with
# the Gradle image's JDK 25.
FROM gradle:jdk25 AS java-builder
WORKDIR /build
COPY discord-bot/ ./discord-bot/
RUN cd discord-bot && gradle shadowJar --no-daemon -q

# ── JRE layer (extracted from JDK image) ────────────────────────────────────
FROM eclipse-temurin:25-jre AS jre

# ── shared base ───────────────────────────────────────────────────────────────
FROM python:3.14-slim AS base

ARG DEBIAN_FRONTEND=noninteractive

# ffmpeg converts every input to 16 kHz mono WAV (and backs pydub)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy Java 25 JRE for the JDA sidecar
COPY --from=jre /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH=$JAVA_HOME/bin:$PATH

WORKDIR /app

# Copy the JDA sidecar fat JAR from the java-builder stage
COPY --from=java-builder /build/discord-bot/build/libs/discord-bot-all.jar ./discord-bot/

# Copy package definition and source tree
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Speaker profiles, config, HF cache, and audio I/O are bind-mounted at
# runtime — no user data is baked into the image.
# WISPER_DATA_DIR is the data dir (config.toml, wisper.db, voice clips,
# journals), overriding the platformdirs default of ~/.local/share/....
ENV WISPER_DATA_DIR=/data

# ── cpu target ────────────────────────────────────────────────────────────────
FROM base AS cpu

# CPU torch/torchaudio first: on PyPI, Linux torch wheels depend on ~3 GB of
# nvidia-* CUDA libraries, which the package install would otherwise pull in.
RUN pip install --no-cache-dir \
        "torch>=2.8.0" \
        "torchaudio>=2.8.0" \
        --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -e . \
 # The app rebuilds the CSS at startup when input.css looks newer, and COPY
 # mtimes are arbitrary; building here means startup never needs the network.
 && python -m wisper_transcribe.tailwind

ENTRYPOINT ["wisper"]
CMD ["--help"]

# ── gpu target ────────────────────────────────────────────────────────────────
FROM base AS gpu

# CUDA 12.6 torch/torchaudio before the package (as setup.ps1 does): PyPI's
# Linux torch carries its own CUDA 13 nvidia-* libraries, which would be
# installed alongside and never used.
RUN pip install --no-cache-dir \
        "torch>=2.8.0" \
        "torchaudio>=2.8.0" \
        --index-url https://download.pytorch.org/whl/cu126 \
 && pip install --no-cache-dir -e . \
 # The app rebuilds the CSS at startup when input.css looks newer, and COPY
 # mtimes are arbitrary; building here means startup never needs the network.
 && python -m wisper_transcribe.tailwind

ENTRYPOINT ["wisper"]
CMD ["--help"]
