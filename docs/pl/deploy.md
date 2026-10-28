# Wdrożenie -- Pipe

Pipe v0.49.2

Pipe działa w trzech trybach (`PIPE_RUNTIME`, domyślnie wykrywany automatycznie). Od trybu zależy,
jak agent widzi zarządzaną maszynę -- reszta (narzędzia, pamięć, klienci, protokół) jest taka sama.

| Tryb | Kiedy | Jak widzi hosta | Instalacja |
|------|-------|-----------------|------------|
| `docker` | VPS, domyślnie | host pod `/hostfs` (ro, `/root` rw), `/proc` hosta pod `/hostproc`, `docker.sock` | `scripts/install-server.sh` |
| `native` | serwer bez Dockera, LXC, mały VPS | bezpośrednio | `scripts/install-server.sh --mode native` |
| `kubernetes` | klaster (k3s, EKS, GKE, AKS...) | klaster przez kubectl (ServiceAccount); opcjonalnie węzeł | `kubectl apply -k deploy/kubernetes/...` |

Obraz Dockera budowany jest dla **amd64 i arm64** (Hetzner CAX, Oracle Ampere, AWS Graviton, Raspberry Pi).

---

## Docker (VPS)

```bash
git clone https://github.com/Kvmyk/pipe && cd pipe
sudo bash scripts/install-server.sh --lang pl
```

Skrypt instaluje Dockera z repozytorium dystrybucji (apt, dnf, apk, pacman, zypper), uruchamia kreator
providera LLM i uruchamia kontenery. Bot Telegram startuje, gdy istnieje `clients/telegram/.env`.

Obraz pochodzi z wydania: `ghcr.io/kvmyk/pipe:<wersja z pliku VERSION>` (amd64 i arm64, podpisany cosign w CI,
polecenie weryfikacji jest w notatkach wydania na GitHubie). Gdy takiego obrazu nie ma (wersja bez wydania, brak
dostępu do ghcr.io) albo podasz `--build`, obraz jest budowany na serwerze ze źródeł. `/aktualizuj` robi to samo.

Ręcznie: `python3 -m backend.configure && cd backend && PIPE_VERSION=$(cat ../VERSION) docker compose up -d`
(bez `PIPE_VERSION`: `docker compose up -d --build` buduje lokalnie obraz z tagiem `local`).

Montowania (`backend/docker-compose.yml`): host `/` -> `/hostfs:ro`, `/root` -> `/hostfs/root` (rw),
`/proc` -> `/hostproc:ro`, `docker.sock`, `/root/.ssh:ro` (git, cele ssh), opcjonalnie `/root/.kube:ro`
(cele-klastry). Port 7379 jest publikowany **tylko na 127.0.0.1** -- dostęp przez tunel SSH; 7380 (webhooki,
działa po ustawieniu `WEBHOOK_PORT` i `WEBHOOK_TOKEN`) -- też tylko na 127.0.0.1. Cały `backend/.env` trafia do
kontenera (`env_file`), więc każde ustawienie z `backend/.env.example` działa bez edycji compose.

`--profile observe` instaluje Pipe w trybie obserwacji: dodaje `backend/docker-compose.observe.yml` (bez
`docker.sock`, Docker tylko do odczytu przez proxy, `/root` tylko do odczytu, bez uprawnień jądra) i ustawia
`PIPE_OBSERVE=1`. Pipe pilnuje i odpowiada, ale niczego nie zmienia. `--profile full` wraca do pełnego trybu,
a ponowna instalacja bez `--profile` zostawia ten, który jest. Szczegóły: [security.md](security.md#tryb-obserwacji-pipe_observe1).

## Native (systemd, bez Dockera)

```bash
sudo bash scripts/install-server.sh --mode native --lang pl
```

Tworzy `.venv`, instaluje zależności, ustawia w `backend/.env` `PIPE_RUNTIME=native`, `TCP_HOST=127.0.0.1`,
`DATA_DIR` i `AUDIT_LOG_PATH` w katalogu instalacji, instaluje `deploy/systemd/pipe.service`
(i `pipe-telegram.service`, jeśli jest `.env` bota). Logi: `journalctl -u pipe -f`.

W tym trybie `cron_manage` może edytować crontab, a `systemctl`/`apt` działają na serwerze (z potwierdzeniem).
Preset Ollamy (`host.docker.internal`) jest automatycznie zamieniany na `127.0.0.1`.

## Kubernetes

> Eksperymentalne: działa i ma testy, ale ma za sobą mniej użycia na prawdziwych serwerach niż reszta Pipe.
> Jeśli coś nie gra, zgłoś to w issue.

[deploy/kubernetes/README.md](../../deploy/kubernetes/README.md) -- baza (zarządzanie klastrem, RBAC tylko
do odczytu, bez sekretów) i nakładki: `operator` (zmiany w klastrze), `telegram` (bot jako sidecar),
`host-agent` (także węzeł, jak VPS). CLI łączy się przez `pipe --kube pipe` (kubectl port-forward).

---

## Nowy serwer w chmurze (cloud-init)

[deploy/cloud-init/user-data.yaml](../../deploy/cloud-init/user-data.yaml) działa u każdego dostawcy
z cloud-init. Uzupełnij `LLM_PROVIDER` i `LLM_API_KEY` (opcjonalnie token bota) i podaj plik przy
tworzeniu serwera. Po ~3 minutach: `ssh root@IP tail /var/log/pipe-install.log`, na laptopie `pipe --host root@IP`.

| Dostawca | Przykład |
|----------|----------|
| Hetzner | `hcloud server create --name pipe --type cax11 --image ubuntu-24.04 --ssh-key moj --user-data-from-file user-data.yaml` |
| DigitalOcean | `doctl compute droplet create pipe --image ubuntu-24-04-x64 --size s-1vcpu-2gb --region fra1 --ssh-keys ID --user-data-file user-data.yaml` |
| AWS EC2 | `aws ec2 run-instances --image-id ami-... --instance-type t4g.small --key-name moj --user-data file://user-data.yaml` |
| Google Cloud | `gcloud compute instances create pipe --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud --metadata-from-file user-data=user-data.yaml` |
| Azure | `az vm create -n pipe -g grupa --image Ubuntu2404 --custom-data user-data.yaml --ssh-key-values ~/.ssh/id_ed25519.pub` |
| OVH / Oracle / Vultr / Linode / Scaleway | pole *user data* / *cloud-init* w panelu albo odpowiednik w CLI |

Dostawca przechowuje user-data w metadanych serwera -- klucz API jest tam widoczny dla kont z dostępem
do projektu. Jeśli to istotne, po instalacji zmień klucz albo skonfiguruj go ręcznie przez SSH.

---

## Konfiguracja bez pytań

`python3 -m backend.configure --from-env [--test]` przepisuje do `backend/.env` ustawione zmienne:
`LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_REASONING_EFFORT`, `LLM_TIMEOUT`,
`AGENT_TOKEN`, `WORKER_MODEL`, `PIPE_RUNTIME`, `PIPE_LANG`, `TCP_HOST`, `WATCH_*`, `VIBE_EVERY` -- i sprawdza, czy
konfiguracja LLM jest kompletna (`--test` od razu testuje tool calling). Używają go `install-server.sh -y`
i cloud-init; przyda się w Ansible czy CI.

## Strefa czasowa

Godziny w przypomnieniach (*"przypomnij o 9"*), rutynach, porannym raporcie (`DIGEST_TIME`) i alertach to czas
**serwera**. W Dockerze kontenery dostają strefę hosta (`/etc/localtime`); wcześniej chodziły w UTC niezależnie od
hosta. Jeśli serwer jest w UTC, a Ty nie -- ustaw w `backend/.env` np. `TZ=Europe/Warsaw` i zrób
`docker compose up -d` (w trybie native: `timedatectl set-timezone Europe/Warsaw`; w Kubernetesie dodaj `TZ` do
sekretów `pipe-env` i `pipe-telegram`). `/przypomnienia` pokazuje aktualny czas i strefę serwera.

## Język (`PIPE_LANG`)

`en` (domyślnie) albo `pl`. Interaktywny kreator pyta o język jako pierwszy; bez pytań: `--lang pl` albo zmienna
`PIPE_LANG=pl` przy `install-server.sh` (instalator przepisuje ją też do `clients/telegram/.env`). W Kubernetesie
dodaj `--from-literal=PIPE_LANG=pl` do sekretów `pipe-env` i `pipe-telegram`. Na działającej instalacji wystarczy
dopisać `PIPE_LANG=pl` do obu plików `.env` i zrestartować backend oraz bota albo wpisać `/jezyk pl`.
Instalacje, których `.env` nie ma `PIPE_LANG` (postawione przed v0.49 bez kreatora), po aktualizacji mówią
po angielsku; `PIPE_LANG=pl` albo `/jezyk pl` przywraca polski.

## Budowanie obrazów

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t REJESTR/pipe-backend:TAG -f backend/Dockerfile --push .
docker buildx build --platform linux/amd64,linux/arm64 -t REJESTR/pipe-telegram:TAG \
    -f clients/telegram/Dockerfile --push .
```

Argumenty obrazu backendu: `INSTALL_KUBECTL=1` (domyślnie; `0` zmniejsza obraz), `KUBECTL_VERSION=stable`,
`DOCKER_VERSION`. Obraz backendu buduje się z korzenia repozytorium (zawiera też stronę `pipe web` dla `/web wlacz`);
`backend/Dockerfile.dockerignore` wpuszcza do niego tylko `backend/` i `clients/webui/`, bez `.env`, `data/` i testów.

## Aktualizacja

```bash
cd /opt/pipe && git pull && sudo bash scripts/install-server.sh           # docker
cd /opt/pipe && git pull && sudo bash scripts/install-server.sh --mode native --lang pl
```

Pamięć agenta (`backend/data/`) i `.env` zostają bez zmian.
