<!-- English or Polish. Rules: CONTRIBUTING.md. / Angielski albo polski. Zasady: CONTRIBUTING.md. -->

## What changes for the user / Co się zmienia dla użytkownika



## How you checked it / Jak to sprawdziłeś

- [ ] `pytest backend/tests/ -q` passes / przechodzi
- [ ] new text exists in English and Polish (`tr()`, `docs/` + `docs/pl/`) / nowe teksty są po angielsku i po polsku
- [ ] classifier changes have a test in `backend/tests/test_security*.py` / zmiana w `security.py` ma test
- [ ] sandbox or a real server (describe) / piaskownica albo prawdziwy serwer (opisz)

No version bump and no `docs/changelog.md` entry, that happens at release.
Bez podbijania wersji i bez wpisu w `docs/changelog.md`, to dzieje się przy wydaniu.
