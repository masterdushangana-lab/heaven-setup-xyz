FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Install base dependencies and aria2
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    tar \
    xz-utils \
    aria2 \
    git \
    python3 \
    python3-pip \
    python3-cryptography \
    && rm -rf /var/lib/apt/lists/*

# Static FFmpeg + FFprobe directly from official BtbN builds (with full codecs, libass & animation tuning)
RUN curl -sL https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz \
    | tar -xJ --strip-components=2 -C /usr/local/bin --wildcards '*/bin/ffmpeg' '*/bin/ffprobe' \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe

# Install Python requests
RUN pip install --no-cache-dir --break-system-packages requests

WORKDIR /workspace
CMD ["/bin/bash"]
