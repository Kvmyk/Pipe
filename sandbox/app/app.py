"""Aplikacja "sklepu" w piaskownicy: odpowiada na :port z /etc/shop/app.conf. Blad konfiguracji = brak startu."""

import http.server
import sys
import time

CONFIG = "/etc/shop/app.conf"


def load() -> dict[str, str]:
    values = {}
    with open(CONFIG, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"{NAME} dziala\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args, flush=True)


if __name__ == "__main__":
    config = load()
    NAME = config.get("name", "Sklep")
    try:
        port = int(config["port"])
    except (KeyError, ValueError) as exc:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} BLAD konfiguracji {CONFIG}: port = {config.get('port')!r} ({exc})",
              flush=True)
        sys.exit(1)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} start na porcie {port}", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
