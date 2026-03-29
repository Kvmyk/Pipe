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

# Fake "free" command inside container to override default tool and read /host_cgroup automatically
RUN echo '#!/bin/bash\n\
if [ -f /host_cgroup/memory.current ]; then\n\
    USED=$(cat /host_cgroup/memory.current)\n\
    MAX=$(cat /host_cgroup/memory.max)\n\
elif [ -f /host_cgroup/memory/memory.usage_in_bytes ]; then\n\
    USED=$(cat /host_cgroup/memory/memory.usage_in_bytes)\n\
    MAX=$(cat /host_cgroup/memory/memory.limit_in_bytes)\n\
else\n\
    exec /usr/bin/free "$@"\n\
fi\n\
MB=$(( 1024 * 1024 ))\n\
USED_MB=$(( USED / MB ))\n\
MAX_MB=$(( MAX / MB ))\n\
FREE_MB=$(( MAX_MB - USED_MB ))\n\
echo "               total        used        free      shared  buff/cache   available"\n\
echo "Mem:           ${MAX_MB}Mi         ${USED_MB}Mi        ${FREE_MB}Mi          0Mi          0Mi         ${FREE_MB}Mi"\n\
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
