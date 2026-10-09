# Serwer w pudelku: Debian z nginx i mala aplikacja "sklepu", a w srodku Pipe w trybie native.
# Budowany z korzenia repozytorium (patrz sandbox/run.py):
#   docker build -f sandbox/Dockerfile -t pipe-sandbox .
# Kontener udaje VPS — Pipe zarzadza nim tak jak prawdziwym serwerem. Zadnych montowan hosta.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx procps curl iproute2 less psmisc \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

WORKDIR /opt/pipe
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY VERSION VERSION
COPY backend backend
COPY sandbox /opt/sandbox
RUN chmod +x /opt/sandbox/start.sh /opt/sandbox/shopctl /opt/sandbox/scenarios/*/*.sh \
    && ln -s /opt/sandbox/shopctl /usr/local/bin/shopctl

# Bez klucza Pipe startuje bez modelu (czuwanie, raporty, MCP); rozmowa po podaniu LLM_PROVIDER + LLM_API_KEY.
ENV PIPE_RUNTIME=native \
    LLM_PROVIDER=none \
    TCP_HOST=0.0.0.0 \
    TCP_PORT=7379 \
    AGENT_SOCKET=/run/pipe.sock \
    DATA_DIR=/var/lib/pipe \
    AUDIT_LOG_PATH=/var/lib/pipe/audit.log \
    PYTHONUNBUFFERED=1
EXPOSE 7379
CMD ["/opt/sandbox/start.sh"]
