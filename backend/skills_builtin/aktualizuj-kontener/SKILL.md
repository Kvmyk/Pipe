---
name: aktualizuj-kontener
description: Aktualizacja obrazu uslugi docker compose (pull + up) z weryfikacja i planem powrotu do poprzedniej wersji.
---

# Aktualizacja uslugi docker compose

## 1. Przed (odczyty)
- Projekt i katalog: DIRECTORY (wpis [compose]) albo docker_manage compose-ps.
- Obecny obraz i jego identyfikator — zapisz je w odpowiedzi (to plan powrotu):
  `docker inspect -f '{{.Config.Image}} {{.Image}}' KONTENER`.
- Czy tag jest przypiety (np. postgres:16.4) czy ruchomy (latest) — przy bazach danych NIE podnos wersji
  glownej bez backupu i zgody uzytkownika (migracja danych!).
- Stan przed: server_history operation=checks (strony odpowiadaja).

## 2. Aktualizacja (po TAK)
`docker compose -f KATALOG/docker-compose.yml pull USLUGA && docker compose -f KATALOG/docker-compose.yml up -d USLUGA`
Bezpiecznik: `docker compose config -q` przed, po — kontenery projektu dzialaja i strony odpowiadaja.

## 3. Po
- Logi: docker_manage logs KONTENER --tail 50 — bledy migracji, restart loop.
- server_history operation=changes since=1h — nowy identyfikator obrazu.

## 4. Powrot do poprzedniej wersji
Ustaw w compose poprzedni tag (albo `image: NAZWA@sha256:...` z kroku 1) i `docker compose up -d USLUGA`.
Obraz sprzed aktualizacji zostaje lokalnie, dopoki nie zrobisz `docker image prune`.
