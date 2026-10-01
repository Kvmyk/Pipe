---
name: free-disk-space
description: Diagnose what fills the disk and safely free space (logs, Docker, package cache), confirming every step.
---

# Free disk space

## 1. Diagnosis (reads, quick)
- system_stats — which disk and what percentage.
- server_history operation=chart metric=disk since=7d — does it grow slowly (data/logs) or in jumps (e.g. dumps).
- Largest directories: `du -xh --max-depth=2 / 2>/dev/null | sort -rh | head -20` (in docker mode: paths under /hostfs).
- Docker: `docker system df`. Journal: `journalctl --disk-usage`. Packages: `du -sh /var/cache/apt`.

## 2. Freeing space — safest first, each step separately (after YES)
1. `journalctl --vacuum-size=200M`
2. `apt-get clean`
3. `docker image prune -f` (unused untagged images), then optionally `docker builder prune -f`
4. Old app logs: `find /var/log -name '*.gz' -mtime +14 -delete` — show the list before deleting (`-print`).
NEVER without an explicit request: `docker volume prune` (data!), deleting backups, `docker system prune -a`.

## 3. Verification
system_stats — how much space was reclaimed. If the problem comes back — suggest log rotation or a routine
(routine_manage) that checks the size of the directory that keeps growing.
