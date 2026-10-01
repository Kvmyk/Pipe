---
name: swap
description: Dodanie pliku swap (np. 2 GB) z wpisem w /etc/fstab — chroni mala maszyne przed zabijaniem procesow przy braku RAM.
---

# Plik swap

## 1. Sprawdz (odczyty)
- system_stats: RAM i obecny swap. Jesli swap juz jest — zapytaj, czy powiekszyc.
- Wolne miejsce: `df -h /` — swap nie moze zabrac wiecej niz ~10% wolnego miejsca.
- Rozmiar: RAM < 2 GB -> swap = RAM; 2-8 GB -> 2-4 GB. Dopytaj, jesli uzytkownik nie podal.
- W trybie docker (SRODOWISKO) nie masz zapisu do / hosta — podaj komendy z kroku 2 do wykonania na hoscie.

## 2. Utworzenie (jedna komenda, po TAK)
```
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && \
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
```
Na btrfs `fallocate` nie dziala dla swapu — uzyj `btrfs filesystem mkswapfile --size 2g /swapfile`.
Bezpiecznik zrobi kopie /etc/fstab i sprawdzi go `findmnt --verify`.

## 3. Dostrojenie (opcjonalnie)
`sysctl vm.swappiness=10 && echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf`

## 4. Weryfikacja
`swapon --show` i system_stats — swap widoczny. Zapisz w SERVER.md (Przeglad): rozmiar swapu.

Cofniecie: `swapoff /swapfile && rm /swapfile`, potem /cofnij dla wpisu fstab.
