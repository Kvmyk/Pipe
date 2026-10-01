---
name: nginx-vhost
description: A new site (domain) in nginx as a reverse proxy to an app, with a Let's Encrypt certificate (certbot).
---

# New domain in nginx + certificate

Collect from the user: the domain (e.g. app.example.com), where to send traffic (app port, e.g. 127.0.0.1:3000),
an e-mail for Let's Encrypt. Do not guess — ask.

## 1. Check the preconditions (reads only)
- DNS: server_history operation=checks or `getent ahosts DOMAIN` — the domain must point at this server.
- Does the app respond locally: `curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:PORT/`.
- Where nginx runs: on the host (`ls /etc/nginx/sites-enabled`) or in a container (docker_manage ps).
- Are ports 80 and 443 public (network_info ports).
- In docker mode (ENVIRONMENT) the host's /etc is read-only — give steps 2-4 to the user
  as commands to run in the server shell instead of running them yourself.

## 2. nginx configuration
Write `/etc/nginx/sites-available/DOMAIN` (write_file — the safety fuse makes a backup and runs `nginx -t`):

```
server {
    listen 80;
    listen [::]:80;
    server_name DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:PORT;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable: `ln -s /etc/nginx/sites-available/DOMAIN /etc/nginx/sites-enabled/DOMAIN && systemctl reload nginx`
(the safety fuse checks `nginx -t` before the reload).

## 3. Certificate
`certbot --nginx -d DOMAIN --non-interactive --agree-tos -m EMAIL --redirect`
No certbot: `apt-get install -y certbot python3-certbot-nginx`.

## 4. Verification
- `curl -sSI https://DOMAIN/` — status 200/301/302, no certificate error.
- server_history operation=checks — certificate valid ~90 days; renewal: `systemctl list-timers | grep certbot`.
- Add the domain to SERVER.md (Domains and network section) and the app directory to DIRECTORY.

Undo: /undo (removes the vhost file and the link, reloads nginx) or `certbot delete --cert-name DOMAIN`.
