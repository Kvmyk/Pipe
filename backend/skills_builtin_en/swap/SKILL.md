---
name: swap
description: Add a swap file (e.g. 2 GB) with an /etc/fstab entry — protects a small machine from killed processes when RAM runs out.
---

# Swap file

## 1. Check (reads)
- system_stats: RAM and current swap. If swap already exists — ask whether to enlarge it.
- Free space: `df -h /` — swap must not take more than ~10% of the free space.
- Size: RAM < 2 GB -> swap = RAM; 2-8 GB -> 2-4 GB. Ask if the user did not say.
- In docker mode (ENVIRONMENT) you cannot write to the host's / — give the commands from step 2 to be run on the host.

## 2. Create (one command, after YES)
```
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && \
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
```
On btrfs `fallocate` does not work for swap — use `btrfs filesystem mkswapfile --size 2g /swapfile`.
The safety fuse backs up /etc/fstab and checks it with `findmnt --verify`.

## 3. Tuning (optional)
`sysctl vm.swappiness=10 && echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf`

## 4. Verification
`swapon --show` and system_stats — swap is visible. Note the swap size in SERVER.md (Overview).

Undo: `swapoff /swapfile && rm /swapfile`, then /undo for the fstab entry.
