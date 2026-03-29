FROM python:3.11-slim

# /app jest rootem — backend jest paczką wewnątrz /app/backend/
# Dzięki temu "from backend.config import settings" działa poprawnie
WORKDIR /app

# Zainstaluj zależności systemowe (pobieramy oficjalny statyczny plik dockera, gwarancja braku objętości pakietu docker.io)
RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    net-tools \
    curl \
    ca-certificates \
    git \
    dnsutils \
    iputils-ping \
    && curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-24.0.9.tgz -o docker.tgz \
    && tar xzvf docker.tgz docker/docker \
    && mv docker/docker /usr/local/bin/docker \
    && rm -rf docker docker.tgz \
    && rm -rf /var/lib/apt/lists/*

# Fake "free" command inside container to override default tool and read from teleported Host stats
RUN echo '#!/bin/bash\n\
MB=$(( 1024 * 1024 ))\n\
MEM_FILE="/hostfs/tmp/vps_meminfo"\n\
if [ ! -f "$MEM_FILE" ]; then MEM_FILE="/proc/meminfo"; fi\n\
TOTAL_KB=$(grep MemTotal "$MEM_FILE" | awk '"'"'{print $2}'"'"')\n\
AVAIL_KB=$(grep MemAvailable "$MEM_FILE" | awk '"'"'{print $2}'"'"')\n\
USED_KB=$(( TOTAL_KB - AVAIL_KB ))\n\
TOTAL_MB=$(( TOTAL_KB / 1024 ))\n\
USED_MB=$(( USED_KB / 1024 ))\n\
FREE_MB=$(( AVAIL_KB / 1024 ))\n\
echo "               total        used        free      shared  buff/cache   available"\n\
echo "Mem:           ${TOTAL_MB}M         ${USED_MB}M        ${FREE_MB}M          0M          0M         ${FREE_MB}M"\n\
echo "Swap:             0B          0B          0B"\n\
' > /usr/local/bin/free && chmod +x /usr/local/bin/free

# Skopiuj i zainstaluj zależności Pythona
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Skopiuj kod backendu do /app/backend/ (żeby paczka "backend" była widoczna)
COPY . ./backend/

# Usuwamy vpsagent, żeby agent mógł bez problemu łączyć się z /var/run/docker.sock jako root wewnątrz kontenera.
# USER vpsagent - usunięto dla kompatybilności z docker.sock
# Punkt wejścia — python widzi /app, więc "import backend" działa
CMD ["python", "-m", "backend.server"]
