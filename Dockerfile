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
FROM gradle:8-jdk25 AS java-builder

WORKDIR /build
COPY discord-bot/ ./discord-bot/
RUN cd discord-bot && gradle shadowJar --no-daemon -q

# ── shared base ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

ARG DEBIAN_FRONTEND=noninteractive

# ffmpeg is required by pydub; curl is used to download vendored HTMX at build time
# openjdk-25-jre-headless runs the JDA sidecar JAR for Discord recording
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        openjdk-25-jre-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the JDA sidecar fat JAR from the java-builder stage
COPY --from=java-builder /build/discord-bot/build/libs/discord-bot-all.jar ./discord-bot/

# Copy package definition and source tree
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Speaker profiles, config, HF cache, and audio I/O are bind-mounted at
# runtime — no user data is baked into the image.
# WISPER_DATA_DIR tells wisper-transcribe where to store config.toml and
# speaker profiles (overrides the platformdirs default of ~/.local/share/...).
ENV WISPER_DATA_DIR=/data

# ── cpu target ────────────────────────────────────────────────────────────────
FROM base AS cpu

RUN pip install --no-cache-dir -e . \
 # Download vendored HTMX so wisper server works fully offline
 && curl -sL "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js" \
         -o /app/src/wisper_transcribe/static/htmx.min.js \
 # Build Tailwind CSS so the web UI is fully self-contained in the image
 && python -m pytailwindcss \
         -i /app/src/wisper_transcribe/static/input.css \
         -o /app/src/wisper_transcribe/static/tailwind.min.css \
         --minify

ENTRYPOINT ["wisper"]
CMD ["--help"]

# ── gpu target ────────────────────────────────────────────────────────────────
FROM base AS gpu

# Install the package (brings in CPU torch as a transitive dep via PyPI),
# then upgrade torch/torchaudio to the CUDA 12.6 builds.
# --upgrade replaces the CPU wheels without touching other installed packages.
RUN pip install --no-cache-dir -e . \
 && pip install --no-cache-dir --upgrade \
        "torch>=2.8.0" \
        "torchaudio>=2.8.0" \
        --index-url https://download.pytorch.org/whl/cu126 \
 # Download vendored HTMX so wisper server works fully offline
 && curl -sL "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js" \
         -o /app/src/wisper_transcribe/static/htmx.min.js \
 # Build Tailwind CSS so the web UI is fully self-contained in the image
 && python -m pytailwindcss \
         -i /app/src/wisper_transcribe/static/input.css \
         -o /app/src/wisper_transcribe/static/tailwind.min.css \
         --minify

ENTRYPOINT ["wisper"]
CMD ["--help"]
