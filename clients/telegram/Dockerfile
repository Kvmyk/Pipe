FROM python:3.11-slim

# /app/clients/telegram będzie naszym katalogiem domowym
WORKDIR /app/clients/telegram

# Systemowe pakiety jeśli wymagane
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Kopiuj same wymagania zeby skorzytac z cache warstwy dockera
COPY clients/telegram/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Skopiuj caly katalog telegram jako /app/clients/telegram
COPY clients/telegram/ .

# Telegram uruchamia się nie uprzywilejowany (ale i tak to tylko powiadomienia)
# Ze względu na Unix Socket lepiej odpalić z roota lub tej samej grupy by łatwo połączyć z plikiem sock stworzonym przez backend
CMD ["python", "telegram.py"]
