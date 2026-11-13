---
name: audyt-iso27001
description: Raport dowodow (tylko odczyt) dla technicznych zabezpieczen ISO/IEC 27001:2022 z Aneksu A, ktore widac na serwerze (dostep, podatnosci, kopie, logi, siec, TLS). To nie certyfikacja.
---

# Dowody ISO/IEC 27001:2022 (tylko odczyt)

Zbierz z tego serwera dowody techniczne dla wybranych zabezpieczen z Aneksu A i opisz je w raporcie.
NICZEGO nie zmieniaj: tylko odczyty. Jedyny dozwolony zapis to zapisanie raportu przez write_file, i tylko gdy
uzytkownik o to poprosi (przechodzi przez potwierdzenie).

## Zasady raportu
- Na poczatku napisz: "Techniczny przeglad dowodow dla wybranych zabezpieczen ISO/IEC 27001:2022 z Aneksu A. To nie
  jest certyfikacja, pelny audyt ISMS ani stwierdzenie zgodnosci. Zabezpieczenia organizacyjne, osobowe i fizyczne
  (5.x, 6.x, 7.x) sa poza zakresem."
- Nigdy nie pisz, ze serwer albo firma jest "zgodny", "zgodna" czy "certyfikowany". Uzywaj tylko statusow:
  spelnione, czesciowo, luka, brak dowodu.
- Komenda, ktora sie nie udala, jest niedozwolona albo nic nie zwraca = "brak dowodu" (z powodem). Nie zgaduj.
- Nigdy nie cytuj sekretow, hashy hasel, kluczy ani tokenow. Wystarcza odciski i liczby.
- Naglowek: data i godzina (`date -Is`) i nazwa hosta (`hostname -f`, w razie bledu `hostname`), wersja Pipe,
  jesli znana.
- Okresy przechowywania (logi, kopie) i kto ma miec dostep to decyzje polityki: oznacz je "do potwierdzenia przez
  osobe odpowiedzialna" i nigdy nie rozstrzygaj ich sam.

## Kroki
0. Naglowek: `date -Is`, `hostname -f`, `cat /etc/os-release`, `uptime -s`.
1. security_audit: ocena i znaleziska; wiekszosc zabezpieczen ponizej bierze z niego dowody.
2. Dowody dla zabezpieczen:
   - A.8.5 Bezpieczne uwierzytelnianie: z audytu logowanie haslem w SSH i logowanie roota; ostatnie logowania kluczem:
     `grep -h 'Accepted' /var/log/auth.log /var/log/secure 2>/dev/null | tail -5` (albo `journalctl -u ssh -u sshd -n 50`).
   - A.5.15 / A.5.18 / A.8.2 Prawa dostepu i dostep uprzywilejowany: konta z powloka logowania
     `getent passwd | awk -F: '$7 !~ /(nologin|false)$/ {print $1, $3, $7}'`, czlonkowie `getent group sudo wheel`,
     odciski kluczy SSH i nowi uzytkownicy z server_history (ostatnie 30 dni).
   - A.8.7 Ochrona przed malware: tylko czy skaner jest zainstalowany i dziala (np. clamav); brak = "brak dowodu".
   - A.8.8 Zarzadzanie podatnosciami technicznymi: znaleziska podatnosci z security_audit (najpierw KEV i krytyczne),
     automatyczne aktualizacje (unattended-upgrades / dnf-automatic), czekajacy restart (`/run/reboot-required`).
   - A.8.9 Zarzadzanie konfiguracja i A.8.32 Zarzadzanie zmianami: server_history (zmiany z 30 dni) i journal
     (co zmienil Pipe, kiedy, jak cofnac). Zmiany zrobione poza Pipe widac tylko w server_history.
   - A.8.13 Kopie zapasowe: wpisy DIRECTORY rodzaju [backup] i alerty o kopiach w prompcie. Bez nich szukaj sladow:
     `ls -la /var/backups`, `crontab -l`, `ls /etc/cron.d /etc/cron.daily`, `systemctl list-timers --all`
     (bez systemd pomin). Podaj najnowszy plik kopii i jego wiek (`ls -lt --time-style=long-iso <katalog> | head`).
     Nic nie ma = luka. Kopia starsza niz jej harmonogram = czesciowo. Testow odtwarzania stad nie widac: "brak dowodu".
   - A.8.15 Rejestrowanie zdarzen: trwaly journald (`ls -d /var/log/journal`), aktywny rsyslog / auditd, logrotate
     (`ls /etc/logrotate.d`); okres przechowywania do potwierdzenia przez osobe odpowiedzialna.
   - A.8.16 Monitorowanie: czuwanie Pipe (alerty w prompcie, poranny raport), webhooki, jesli sa.
   - A.8.17 Synchronizacja zegara: `timedatectl show -p NTPSynchronized -p Timezone`
     (bez systemd: `ps -e | grep -E 'chrony|ntp'`).
   - A.8.20 / A.8.21 Bezpieczenstwo sieci: zapora z audytu, nasluchujace porty `ss -tlnpu`, porty opublikowane przez
     kontenery (`docker ps --format '{{.Names}} {{.Ports}}'`, jesli jest docker).
   - A.8.24 Kryptografia: waznosc certyfikatow i HTTPS z audytu i alertow, algorytmy SSH
     `sshd -T 2>/dev/null | grep -iE '^(ciphers|kexalgorithms|macs) '`.
3. Raport jako tabela: zabezpieczenie | status | dowod (komenda + krotki wynik) | co zrobic. Poprawki wskazuja
   istniejace narzedzia: `/zalataj` (security_patch), skille utwardz-ssh, fail2ban-ssh, backup-postgres. Potem krotka
   lista luk, najwazniejsze najpierw, i lista punktow do potwierdzenia przez osobe odpowiedzialna.
4. Zapytaj, czy zapisac raport (np. /root/dowody-iso27001-<data>.md). Dopiero po zgodzie: write_file.
