---
name: wolne-miejsce
description: Diagnoza, co zajmuje dysk, i bezpieczne zwolnienie miejsca (logi, Docker, cache pakietow) z potwierdzeniem kazdego kroku.
---

# Wolne miejsce na dysku

## 1. Diagnoza (odczyty, szybkie)
- system_stats — ktory dysk i ile procent.
- server_history operation=chart metric=disk since=7d — czy rosnie powoli (dane/logi) czy skokowo (np. dumpy).
- Najwieksze katalogi: `du -xh --max-depth=2 / 2>/dev/null | sort -rh | head -20` (w trybie docker: sciezki pod /hostfs).
- Docker: `docker system df`. Dziennik: `journalctl --disk-usage`. Pakiety: `du -sh /var/cache/apt`.

## 2. Zwalnianie — od najbezpieczniejszego, kazdy krok osobno (po TAK)
1. `journalctl --vacuum-size=200M`
2. `apt-get clean`
3. `docker image prune -f` (nieuzywane obrazy bez tagu), potem ewentualnie `docker builder prune -f`
4. Stare logi aplikacji: `find /var/log -name '*.gz' -mtime +14 -delete` — pokaz liste przed usunieciem (`-print`).
NIGDY bez wyraznej prosby: `docker volume prune` (dane!), usuwanie backupow, `docker system prune -a`.

## 3. Weryfikacja
system_stats — ile miejsca odzyskano. Jesli problem wroci — zaproponuj rotacje logow albo rutyne
(routine_manage) sprawdzajaca rozmiar katalogu, ktory rosnie.
