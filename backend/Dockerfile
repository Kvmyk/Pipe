FROM python:3.11-slim

# /app jest rootem — backend jest paczką wewnątrz /app/backend/
# Dzięki temu "from backend.config import settings" działa poprawnie
WORKDIR /app

# Obraz wieloarchitekturowy: docker buildx build --platform linux/amd64,linux/arm64 ...
# (VPS-y ARM: Hetzner CAX, Oracle Ampere, AWS Graviton, Raspberry Pi)
ARG TARGETARCH
ARG DOCKER_VERSION=27.5.1
# kubectl dla trybu kubernetes i celow-klastrow; "stable" = najnowsza wersja w chwili budowania
ARG INSTALL_KUBECTL=1
ARG KUBECTL_VERSION=stable

# Narzędzia systemowe + statyczny klient Dockera (bez demona) + opcjonalnie kubectl
RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    net-tools \
    curl \
    ca-certificates \
    git \
    openssh-client \
    dnsutils \
    iputils-ping \
    && ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && case "$ARCH" in amd64) DARCH=x86_64 ;; arm64) DARCH=aarch64 ;; *) DARCH="$ARCH" ;; esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${DARCH}/docker-${DOCKER_VERSION}.tgz" -o docker.tgz \
    && tar xzf docker.tgz docker/docker \
    && mv docker/docker /usr/local/bin/docker \
    && rm -rf docker docker.tgz \
    && if [ "$INSTALL_KUBECTL" = "1" ]; then \
         V="$KUBECTL_VERSION"; [ "$V" = "stable" ] && V="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"; \
         curl -fsSL "https://dl.k8s.io/release/${V}/bin/linux/${ARCH}/kubectl" -o /usr/local/bin/kubectl \
         && chmod +x /usr/local/bin/kubectl; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# "free" w kontenerze pokazuje RAM hosta — czyta /proc hosta (HOST_PROC, domyślnie /hostproc)
RUN printf '%s\n' \
    '#!/bin/sh' \
    'MEM_FILE="${HOST_PROC:-/hostproc}/meminfo"' \
    '[ -f "$MEM_FILE" ] || MEM_FILE=/proc/meminfo' \
    'awk '"'"'/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} /^SwapTotal:/{st=$2} /^SwapFree:/{sf=$2} END {' \
    '  printf "               total        used        free      shared  buff/cache   available\n";' \
    '  printf "Mem:     %10dM %10dM %10dM %10dM %10dM %10dM\n", t/1024, (t-a)/1024, a/1024, 0, 0, a/1024;' \
    '  printf "Swap:    %10dM %10dM %10dM\n", st/1024, (st-sf)/1024, sf/1024 }'"'"' "$MEM_FILE"' \
    > /usr/local/bin/free && chmod +x /usr/local/bin/free

# Skopiuj i zainstaluj zależności Pythona
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Skopiuj kod backendu do /app/backend/ (żeby paczka "backend" była widoczna)
COPY . ./backend/

# Działa jako root w kontenerze: potrzebny dostęp do /var/run/docker.sock i /hostfs/root.
ENV PYTHONUNBUFFERED=1
EXPOSE 7379
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 7379), 3).close()" || exit 1
CMD ["python", "-m", "backend.server"]
