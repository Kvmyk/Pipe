# Wdrozenie -- Pipe

Pipe v0.21.1

Pipe dziala w trzech trybach (`PIPE_RUNTIME`, domyslnie wykrywany automatycznie). Od trybu zalezy,
jak agent widzi zarzadzana maszyne -- reszta (narzedzia, pamiec, klienci, protokol) jest taka sama.

| Tryb | Kiedy | Jak widzi hosta | Instalacja |
|------|-------|-----------------|------------|
| `docker` | VPS, domyslnie | host pod `/hostfs` (ro, `/root` rw), `/proc` hosta pod `/hostproc`, `docker.sock` | `scripts/install-server.sh` |
| `native` | serwer bez Dockera, LXC, maly VPS | bezposrednio | `scripts/install-server.sh --mode native` |
| `kubernetes` | klaster (k3s, EKS, GKE, AKS...) | klaster przez kubectl (ServiceAccount); opcjonalnie wezel | `kubectl apply -k deploy/kubernetes/...` |

Obraz Dockera budowany jest dla **amd64 i arm64** (Hetzner CAX, Oracle Ampere, AWS Graviton, Raspberry Pi).

---

## Docker (VPS)

```bash
git clone https://github.com/user/pipe && cd pipe
sudo bash scripts/install-server.sh
```

Skrypt instaluje Dockera z repozytorium dystrybucji (apt, dnf, apk, pacman, zypper), uruchamia kreator
providera LLM i `docker compose up -d --build`. Bot Telegram startuje, gdy istnieje `clients/telegram/.env`.

Recznie: `python3 -m backend.configure && cd backend && docker compose up -d --build`.

Montowania (`backend/docker-compose.yml`): host `/` -> `/hostfs:ro`, `/root` -> `/hostfs/root` (rw),
`/proc` -> `/hostproc:ro`, `docker.sock`, `/root/.ssh:ro` (git, cele ssh), opcjonalnie `/root/.kube:ro`
(cele-klastry). Port 7379 jest publikowany **tylko na 127.0.0.1** -- dostep przez tunel SSH; 7380 (webhooki,
dziala po ustawieniu `WEBHOOK_PORT` i `WEBHOOK_TOKEN`) -- tez tylko na 127.0.0.1. Caly `backend/.env` trafia do
kontenera (`env_file`), wiec kazde ustawienie z `backend/.env.example` dziala bez edycji compose.

## Native (systemd, bez Dockera)

```bash
sudo bash scripts/install-server.sh --mode native
```

Tworzy `.venv`, instaluje zaleznosci, ustawia w `backend/.env` `PIPE_RUNTIME=native`, `TCP_HOST=127.0.0.1`,
`DATA_DIR` i `AUDIT_LOG_PATH` w katalogu instalacji, instaluje `deploy/systemd/pipe.service`
(i `pipe-telegram.service`, jesli jest `.env` bota). Logi: `journalctl -u pipe -f`.

W tym trybie `cron_manage` moze edytowac crontab, a `systemctl`/`apt` dzialaja na serwerze (z potwierdzeniem).
Preset Ollamy (`host.docker.internal`) jest automatycznie zamieniany na `127.0.0.1`.

## Kubernetes

[deploy/kubernetes/README.md](../deploy/kubernetes/README.md) -- baza (zarzadzanie klastrem, RBAC tylko
do odczytu, bez sekretow) i nakladki: `operator` (zmiany w klastrze), `telegram` (bot jako sidecar),
`host-agent` (takze wezel, jak VPS). CLI laczy sie przez `pipe --kube pipe` (kubectl port-forward).

---

## Nowy serwer w chmurze (cloud-init)

[deploy/cloud-init/user-data.yaml](../deploy/cloud-init/user-data.yaml) dziala u kazdego dostawcy
z cloud-init. Uzupelnij `LLM_PROVIDER` i `LLM_API_KEY` (opcjonalnie token bota) i podaj plik przy
tworzeniu serwera. Po ~3 minutach: `ssh root@IP tail /var/log/pipe-install.log`, na laptopie `pipe --host root@IP`.

| Dostawca | Przyklad |
|----------|----------|
| Hetzner | `hcloud server create --name pipe --type cax11 --image ubuntu-24.04 --ssh-key moj --user-data-from-file user-data.yaml` |
| DigitalOcean | `doctl compute droplet create pipe --image ubuntu-24-04-x64 --size s-1vcpu-2gb --region fra1 --ssh-keys ID --user-data-file user-data.yaml` |
| AWS EC2 | `aws ec2 run-instances --image-id ami-... --instance-type t4g.small --key-name moj --user-data file://user-data.yaml` |
| Google Cloud | `gcloud compute instances create pipe --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud --metadata-from-file user-data=user-data.yaml` |
| Azure | `az vm create -n pipe -g grupa --image Ubuntu2404 --custom-data user-data.yaml --ssh-key-values ~/.ssh/id_ed25519.pub` |
| OVH / Oracle / Vultr / Linode / Scaleway | pole *user data* / *cloud-init* w panelu albo odpowiednik w CLI |

Dostawca przechowuje user-data w metadanych serwera -- klucz API jest tam widoczny dla kont z dostepem
do projektu. Jesli to istotne, po instalacji zmien klucz albo skonfiguruj go recznie przez SSH.

---

## Konfiguracja bez pytan

`python3 -m backend.configure --from-env [--test]` przepisuje do `backend/.env` ustawione zmienne:
`LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_REASONING_EFFORT`, `LLM_TIMEOUT`,
`AGENT_TOKEN`, `WORKER_MODEL`, `PIPE_RUNTIME`, `PIPE_LANG`, `TCP_HOST`, `WATCH_*`, `VIBE_EVERY` -- i sprawdza, czy
konfiguracja LLM jest kompletna (`--test` od razu testuje tool calling). Uzywaja go `install-server.sh -y`
i cloud-init; przyda sie w Ansible czy CI.

## Strefa czasowa

Godziny w przypomnieniach (*"przypomnij o 9"*), rutynach, porannym raporcie (`DIGEST_TIME`) i alertach to czas
**serwera**. W Dockerze kontenery dostaja strefe hosta (`/etc/localtime`); wczesniej chodzily w UTC niezaleznie od
hosta. Jesli serwer jest w UTC, a Ty nie -- ustaw w `backend/.env` np. `TZ=Europe/Warsaw` i zrob
`docker compose up -d` (w trybie native: `timedatectl set-timezone Europe/Warsaw`; w Kubernetesie dodaj `TZ` do
sekretow `pipe-env` i `pipe-telegram`). `/przypomnienia` pokazuje aktualny czas i strefe serwera.

## Jezyk (`PIPE_LANG`)

`pl` (domyslnie) albo `en`. Interaktywny kreator pyta o jezyk jako pierwszy; bez pytan: `--lang en` albo zmienna
`PIPE_LANG=en` przy `install-server.sh` (instalator przepisuje ja tez do `clients/telegram/.env`). W Kubernetesie
dodaj `--from-literal=PIPE_LANG=en` do sekretow `pipe-env` i `pipe-telegram`. Na dzialajacej instalacji wystarczy
dopisac `PIPE_LANG=en` do obu plikow `.env` i zrestartowac backend oraz bota.

## Budowanie obrazow

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t REJESTR/pipe-backend:TAG --push backend/
docker buildx build --platform linux/amd64,linux/arm64 -t REJESTR/pipe-telegram:TAG \
    -f clients/telegram/Dockerfile --push .
```

Argumenty obrazu backendu: `INSTALL_KUBECTL=1` (domyslnie; `0` zmniejsza obraz), `KUBECTL_VERSION=stable`,
`DOCKER_VERSION`. `backend/.dockerignore` nie wpuszcza do obrazu `.env` ani `data/`.

## Aktualizacja

```bash
cd /opt/pipe && git pull && sudo bash scripts/install-server.sh           # docker
cd /opt/pipe && git pull && sudo bash scripts/install-server.sh --mode native
```

Pamiec agenta (`backend/data/`) i `.env` zostaja bez zmian.
