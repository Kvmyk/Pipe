---
name: fail2ban-ssh
description: Instalacja fail2ban i blokowanie adresow, ktore zgaduja hasla SSH.
---

# fail2ban dla SSH

## 1. Sprawdz (odczyty)
- security_audit: czy logowanie haslem jest wlaczone (wtedy fail2ban jest wazny, przy samych kluczach — mniej).
- Port SSH: `grep -i '^Port' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf` (domyslnie 22).
- Adres, z ktorego laczy sie uzytkownik (zeby go nie zablokowac): `last -n 5 -a` albo `who`.
- W trybie docker podaj komendy z krokow 2-3 do wykonania na hoscie.

## 2. Instalacja
`apt-get install -y fail2ban` (Debian/Ubuntu); `dnf install -y fail2ban` (RHEL/Fedora).

## 3. Konfiguracja — /etc/fail2ban/jail.local (write_file)
```
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5
ignoreip = 127.0.0.1/8 ::1 ADRES_UZYTKOWNIKA

[sshd]
enabled = true
port = PORT_SSH
backend = systemd
```
Na starszych systemach z /var/log/auth.log `backend = auto`.
Potem: `systemctl enable --now fail2ban && systemctl restart fail2ban`.

## 4. Weryfikacja
`fail2ban-client status sshd` — jail aktywny, liczba zablokowanych adresow. Zapisz w SERVER.md.

Odblokowanie adresu: `fail2ban-client set sshd unbanip ADRES`. Cofniecie: /cofnij i `systemctl disable --now fail2ban`.
