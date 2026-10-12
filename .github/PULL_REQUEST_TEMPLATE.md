<!-- Polski albo angielski. Zasady: CONTRIBUTING.md. / Polish or English. Rules: CONTRIBUTING.md. -->

## Co się zmienia dla użytkownika / What changes for the user



## Jak to sprawdziłeś / How you checked it

- [ ] `pytest backend/tests/ -q` przechodzi / passes
- [ ] nowe teksty są po polsku i po angielsku (`tr()`, `docs/` + `docs/en/`) / new text exists in both languages
- [ ] zmiana w `security.py` ma test w `backend/tests/test_security*.py` / classifier changes have a test
- [ ] piaskownica albo prawdziwy serwer (opisz) / sandbox or a real server (describe)

Bez podbijania wersji i bez wpisu w `docs/changelog.md`, to dzieje się przy wydaniu.
No version bump and no `docs/changelog.md` entry, that happens at release.
