---
name: utwardz-ssh
description: Bezpieczne wylaczenie logowania haslem i roota haslem w SSH — w kolejnosci, ktora nie odetnie uzytkownika.
---

# Utwardzenie SSH bez odciecia sie

Kolejnosc jest wazna: najpierw klucz, potem wylaczenie hasel.

## 1. Sprawdz (odczyty)
- security_audit — co jest do poprawy (logowanie haslem, root).
- Czy uzytkownik ma dzialajacy klucz: `ls -la /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys`
  i `last -n 10 -a` — czy ostatnie logowania byly kluczem (`grep 'Accepted publickey' /var/log/auth.log | tail -3`).
- Jesli NIE ma klucza — zatrzymaj sie. Popros uzytkownika o klucz publiczny (`cat ~/.ssh/id_ed25519.pub` na
  laptopie) i dopisz go (write_file do authorized_keys — pokaz tresc do akceptacji).
- Popros uzytkownika, zeby w DRUGIM oknie terminala zalogowal sie kluczem i potwierdzil, ze dziala.

## 2. Zmiana (po TAK)
Uzyj komendy z security_audit (plik /etc/ssh/sshd_config.d/00-pipe-*.conf albo sed na sshd_config).
Bezpiecznik zrobi kopie, `sshd -t` przed przeladowaniem i sprawdzi, ze usluga ssh jest aktywna.
Przeladowanie (reload) nie zrywa istniejacych sesji.

## 3. Weryfikacja
- Uzytkownik otwiera NOWA sesje SSH kluczem — musi dzialac, zanim zamknie stara.
- security_audit — ocena powinna wzrosnac.

Cofniecie: /cofnij (przywraca konfiguracje i przeladowuje sshd).
