# Bezpieczeństwo / Security

Polski poniżej, English below.

## Zgłaszanie podatności

Pipe działa na serwerze z uprawnieniami roota, więc każdą podatność traktuję poważnie. Nie zgłaszaj jej w
publicznym issue. Użyj prywatnego zgłoszenia na GitHubie: zakładka **Security**, przycisk **Report a vulnerability**
(https://github.com/Kvmyk/Pipe/security/advisories/new). Zgłoszenie widzę tylko ja.

Napisz, czego dotyczy błąd, jak go odtworzyć (wersja Pipe, tryb: docker / native / kubernetes, provider LLM, jeśli
ma znaczenie) i co według Ciebie da się dzięki niemu zrobić. Projekt prowadzi jedna osoba, więc nie obiecuję
terminów, ale odpowiadam na każde zgłoszenie. Po wydaniu poprawki opiszę ją w `docs/changelog.md` i, jeśli
chcesz, podziękuję Ci z imienia.

Wspierana jest tylko najnowsza wersja z gałęzi `main` i najnowsze wydanie.

### Co uważam za podatność

- komenda, która zmienia stan serwera, a wykonuje się bez potwierdzenia (obejście klasyfikatora w
  `backend/core/security.py`, workerów albo bramki MCP),
- tekst z zewnątrz (strona z internetu, log, załącznik, alert z webhooka, wynik narzędzia), który sprawia, że
  agent wykonuje zmianę bez Twojego „tak” albo z planem innym niż pokazany,
- wyciek sekretu do providera LLM, do logu, do pamięci agenta albo do innego klienta mimo redakcji,
- obejście tokenów i ról (viewer robi coś, czego nie powinien; sesja innego użytkownika),
- odczyt albo zapis poza dozwolonym obszarem (ścieżki, symlinki, `/proc`),
- trwałe zmiany w pamięci agenta (SERVER.md, skille) bez potwierdzenia,
- problemy w `pipe web` (XSS, CSRF, wyciek tokenu backendu do przeglądarki) i w webhookach.

### Czego nie uważam za podatność

- To, co robi komenda zatwierdzona przez administratora. Prawdziwym zabezpieczeniem jest człowiek, który czyta
  plan przed „tak” (opisane w `docs/security.md`).
- To, że Pipe ma `docker.sock`, czyli w praktyce roota na hoście. To założenie projektu, a nie błąd.
- Tryb YOLO włączony przez administratora, o ile działa tak, jak opisuje dokumentacja.
- Ataki wymagające wcześniejszego roota na serwerze albo dostępu do `backend/.env`.

Model zagrożeń i wszystkie zabezpieczenia: [docs/security.md](docs/security.md)
(po angielsku: [docs/en/security.md](docs/en/security.md)).

### Weryfikacja obrazów

Obrazy z `ghcr.io/kvmyk/pipe` i `ghcr.io/kvmyk/pipe-telegram` buduje i podpisuje (cosign, bez kluczy) workflow
`.github/workflows/release.yml`. Polecenie do weryfikacji jest w notatkach każdego wydania na GitHubie.

---

## Reporting a vulnerability

Pipe runs on a server with root privileges, so I take every vulnerability seriously. Please don't report it in a
public issue. Use GitHub's private reporting: the **Security** tab, **Report a vulnerability**
(https://github.com/Kvmyk/Pipe/security/advisories/new). Only I can see the report.

Describe what is affected, how to reproduce it (Pipe version, mode: docker / native / kubernetes, the LLM provider
if it matters) and what you think it allows. The project has a single maintainer, so I can't promise deadlines, but
I answer every report. After a fix is released I describe it in `docs/changelog.md` and, if you want, credit you
by name.

Only the latest version on `main` and the latest release are supported.

### In scope

- a command that changes the server and runs without confirmation (bypassing the classifier in
  `backend/core/security.py`, workers or the MCP gateway),
- outside text (a web page, a log, an attachment, a webhook alert, tool output) that makes the agent change
  something without your "yes", or with a plan other than the one shown,
- a secret leaking to the LLM provider, a log, the agent's memory or another client despite redaction,
- bypassing tokens and roles (a viewer doing what it shouldn't; another user's session),
- reading or writing outside the allowed area (paths, symlinks, `/proc`),
- persistent changes to the agent's memory (SERVER.md, skills) without confirmation,
- problems in `pipe web` (XSS, CSRF, the backend token reaching the browser) and in webhooks.

### Out of scope

- What a command approved by the administrator does. The real safeguard is the person reading the plan before
  saying "yes" (see `docs/en/security.md`).
- Pipe having `docker.sock`, which is effectively root on the host. That is a design decision, not a bug.
- YOLO mode turned on by the administrator, as long as it works as documented.
- Attacks that need root on the server or access to `backend/.env` first.

Threat model and every safeguard: [docs/en/security.md](docs/en/security.md).

### Verifying images

Images in `ghcr.io/kvmyk/pipe` and `ghcr.io/kvmyk/pipe-telegram` are built and signed (cosign, keyless) by
`.github/workflows/release.yml`. The verification command is in the notes of every GitHub release.
