FROM python:3.11-slim

WORKDIR /app

# Zainstaluj zależności systemowe
RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    net-tools \
    && rm -rf /var/lib/apt/lists/*

# Skopiuj i zainstaluj zależności Pythona
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Skopiuj kod backendu
COPY . .

# Utwórz użytkownika bez uprawnień roota
RUN useradd --system --no-create-home --shell /bin/false vpsagent
USER vpsagent

# Punkt wejścia
CMD ["python", "-m", "backend.server"]
