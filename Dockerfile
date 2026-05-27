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
# Builds the JDA+JDAVE Discord bot fat JAR.  Uses the Gradle image with JDK 25
# pre-installed; the source tree in discord-bot/ is a placeholder that will be
# replaced with the real JDA voice-receive implementation.
FROM gradle:jdk25 AS java-builder
WORKDIR /build
COPY discord-bot/ ./discord-bot/
RUN cd discord-bot && gradle shadowJar --no-daemon -q

# ── JRE layer (extracted from JDK image) ────────────────────────────────────
FROM eclipse-temurin:25-jre AS jre

# ── shared base ───────────────────────────────────────────────────────────────
FROM python:3.14-slim AS base

ARG DEBIAN_FRONTEND=noninteractive

# ffmpeg is required by pydub; curl is used to download vendored HTMX at build time
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
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
# WISPER_DATA_DIR tells wisper-transcribe where to store config.toml and
# speaker profiles (overrides the platformdirs default of ~/.local/share/...).
ENV WISPER_DATA_DIR=/data

# Pin the tailwindcss binary tag so the download URL is
# /releases/download/v4.2.4/... (cached on the GitHub CDN) instead of
# /releases/latest/download/... which is redirect-prone and has hit
# transient HTTP 503/504 in CI builds.
ENV TAILWINDCSS_VERSION=v4.2.4

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
