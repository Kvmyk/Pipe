# Deployment -- Pipe

Pipe v0.28.1

Pipe runs in three modes (`PIPE_RUNTIME`, auto-detected by default). The mode decides how the agent sees the managed
machine; everything else (tools, memory, clients, protocol) is the same.

| Mode | When | How it sees the host | Installation |
|------|------|----------------------|--------------|
| `docker` | VPS, default | host under `/hostfs` (ro, `/root` rw), host `/proc` under `/hostproc`, `docker.sock` | `scripts/install-server.sh` |
| `native` | server without Docker, LXC, small VPS | directly | `scripts/install-server.sh --mode native` |
| `kubernetes` | cluster (k3s, EKS, GKE, AKS...) | the cluster through kubectl (ServiceAccount); optionally the node | `kubectl apply -k deploy/kubernetes/...` |

The Docker image is built for **amd64 and arm64** (Hetzner CAX, Oracle Ampere, AWS Graviton, Raspberry Pi).

---

## Docker (VPS)

```bash
git clone https://github.com/Kvmyk/pipe && cd pipe
sudo bash scripts/install-server.sh --lang en
```

The script installs Docker from the distribution's repository (apt, dnf, apk, pacman, zypper), runs the LLM provider
wizard and starts the containers. The Telegram bot starts when `clients/telegram/.env` exists.

The image comes from a release: `ghcr.io/kvmyk/pipe:<version from the VERSION file>` (amd64 and arm64, signed with
cosign in CI; the verification command is in the GitHub release notes). When there is no such image (a version without
a release, no access to ghcr.io) or you pass `--build`, the image is built on the server from source. `/update` does
the same.

By hand: `python3 -m backend.configure && cd backend && PIPE_VERSION=$(cat ../VERSION) docker compose up -d`
(without `PIPE_VERSION`: `docker compose up -d --build` builds a local image tagged `local`).

Mounts (`backend/docker-compose.yml`): host `/` -> `/hostfs:ro`, `/root` -> `/hostfs/root` (rw),
`/proc` -> `/hostproc:ro`, `docker.sock`, `/root/.ssh:ro` (git, ssh targets), optionally `/root/.kube:ro`
(cluster targets). Port 7379 is published **only on 127.0.0.1**, access goes through an SSH tunnel; 7380 (webhooks,
active once `WEBHOOK_PORT` and `WEBHOOK_TOKEN` are set) is also only on 127.0.0.1. The whole `backend/.env` reaches the
container (`env_file`), so every setting from `backend/.env.example` works without editing compose.

## Native (systemd, without Docker)

```bash
sudo bash scripts/install-server.sh --mode native --lang en
```

Creates `.venv`, installs the dependencies, sets `PIPE_RUNTIME=native`, `TCP_HOST=127.0.0.1`, `DATA_DIR` and
`AUDIT_LOG_PATH` (in the install directory) in `backend/.env`, installs `deploy/systemd/pipe.service`
(and `pipe-telegram.service` when the bot's `.env` exists). Logs: `journalctl -u pipe -f`.

In this mode `cron_manage` can edit the crontab, and `systemctl`/`apt` work on the server (with confirmation).
The Ollama preset (`host.docker.internal`) is automatically replaced with `127.0.0.1`.

## Kubernetes

[deploy/kubernetes/README.md](../../deploy/kubernetes/README.md) -- the base (cluster management, read-only RBAC,
no secrets) and overlays: `operator` (changes in the cluster), `telegram` (the bot as a sidecar),
`host-agent` (also the node, like a VPS). The CLI connects with `pipe --kube pipe` (kubectl port-forward).

---

## New cloud server (cloud-init)

[deploy/cloud-init/user-data.yaml](../../deploy/cloud-init/user-data.yaml) works with every provider that supports
cloud-init. Fill in `LLM_PROVIDER` and `LLM_API_KEY` (optionally the bot token) and pass the file when creating the
server. After about 3 minutes: `ssh root@IP tail /var/log/pipe-install.log`, and on your laptop `pipe --host root@IP`.

| Provider | Example |
|----------|---------|
| Hetzner | `hcloud server create --name pipe --type cax11 --image ubuntu-24.04 --ssh-key mine --user-data-from-file user-data.yaml` |
| DigitalOcean | `doctl compute droplet create pipe --image ubuntu-24-04-x64 --size s-1vcpu-2gb --region fra1 --ssh-keys ID --user-data-file user-data.yaml` |
| AWS EC2 | `aws ec2 run-instances --image-id ami-... --instance-type t4g.small --key-name mine --user-data file://user-data.yaml` |
| Google Cloud | `gcloud compute instances create pipe --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud --metadata-from-file user-data=user-data.yaml` |
| Azure | `az vm create -n pipe -g group --image Ubuntu2404 --custom-data user-data.yaml --ssh-key-values ~/.ssh/id_ed25519.pub` |
| OVH / Oracle / Vultr / Linode / Scaleway | the *user data* / *cloud-init* field in the panel or its CLI equivalent |

The provider keeps user-data in the server's metadata, so the API key is visible there to accounts with access to the
project. If that matters, change the key after installation or configure it by hand over SSH.

---

## Configuration without questions

`python3 -m backend.configure --from-env [--test]` copies the variables that are set into `backend/.env`:
`LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_REASONING_EFFORT`, `LLM_TIMEOUT`,
`AGENT_TOKEN`, `WORKER_MODEL`, `PIPE_RUNTIME`, `PIPE_LANG`, `TCP_HOST`, `WATCH_*`, `VIBE_EVERY`, and checks that the
LLM configuration is complete (`--test` also tests tool calling). `install-server.sh -y` and cloud-init use it; it is
handy in Ansible or CI.

## Time zone

Times in reminders (*"remind me at 9"*), routines, the morning report (`DIGEST_TIME`) and alerts are **server**
time. In Docker the containers take the host's time zone (`/etc/localtime`); before that they ran in UTC regardless of
the host. If the server is in UTC and you are not, set for example `TZ=Europe/Warsaw` in `backend/.env` and run
`docker compose up -d` (in native mode: `timedatectl set-timezone Europe/Warsaw`; in Kubernetes add `TZ` to the
`pipe-env` and `pipe-telegram` secrets). `/reminders` shows the server's current time and time zone.

## Language (`PIPE_LANG`)

`pl` (default) or `en`. The interactive wizard asks for the language first; without questions: `--lang en` or the
`PIPE_LANG=en` variable for `install-server.sh` (the installer also copies it into `clients/telegram/.env`). In
Kubernetes add `--from-literal=PIPE_LANG=en` to the `pipe-env` and `pipe-telegram` secrets. On a running installation
add `PIPE_LANG=en` to both `.env` files and restart the backend and the bot, or use `/language en` at runtime.

## Building images

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t REGISTRY/pipe-backend:TAG --push backend/
docker buildx build --platform linux/amd64,linux/arm64 -t REGISTRY/pipe-telegram:TAG \
    -f clients/telegram/Dockerfile --push .
```

Backend image arguments: `INSTALL_KUBECTL=1` (default; `0` makes the image smaller), `KUBECTL_VERSION=stable`,
`DOCKER_VERSION`. `backend/.dockerignore` keeps `.env` and `data/` out of the image.

## Updating

```bash
cd /opt/pipe && git pull && sudo bash scripts/install-server.sh           # docker
cd /opt/pipe && git pull && sudo bash scripts/install-server.sh --mode native
```

The agent's memory (`backend/data/`) and `.env` stay unchanged. In docker mode `/update` does the same from a
conversation.
