# Piaskownica i scenariusze awarii

Kontener udający mały VPS: Debian, nginx na porcie 80, aplikacja „sklepu” na 8080 (`shopctl start|stop|restart`),
pliki statyczne pod `/static/`, a w środku Pipe w trybie native. Pipe zarządza kontenerem tak jak prawdziwym
serwerem. Nic z Twojego komputera nie jest montowane.

Potrzebujesz Dockera i Pythona 3. Model podajesz zmiennymi jak w `backend/.env` (`LLM_PROVIDER`, `LLM_API_KEY`,
`LLM_MODEL`) albo przez `--env-file backend/.env`. Klucz trafia do kontenera przez `docker run -e NAZWA`, więc nie
widać go na liście procesów.

## Wypróbuj

```bash
python3 sandbox/run.py try                        # sprawny serwer
python3 sandbox/run.py try --scenario app-down    # od razu z awarią
pipe --no-tunnel --local-port 7390                # albo: pipe web --no-tunnel --local-port 7390
docker rm -f pipe-sandbox                         # koniec
```

## Scenariusze

| Scenariusz | Co jest zepsute | Kiedy zdany |
|---|---|---|
| `nginx-502` | `proxy_pass` wskazuje port 8081 zamiast 8080 | `curl localhost/` zwraca „Sklep dziala” |
| `app-down` | literówka w porcie w `/etc/shop/app.conf`, aplikacja nie startuje | jak wyżej |
| `static-403` | `/var/www/shop/static/logo.txt` ma prawa 600 | `/static/logo.txt` zwraca plik |
| `disk-hog` | 64 MB `debug.log.1` w `/var/log/shop` | odpowiedź wskazuje ten plik, a plik dalej istnieje |

Każdy katalog w `scenarios/` ma `scenario.json` (tytuł i polecenie po polsku i angielsku, rodzaj `fix` albo
`diagnose`, słowa, które musi zawierać odpowiedź), `break.sh` (psuje przy starcie), `check.sh` (kod 0 = zdany)
i `fix.sh` (wzorcowa naprawa, używana tylko przez autotest).

## Ocena modeli

```bash
LLM_PROVIDER=gemini LLM_API_KEY=... python3 sandbox/run.py eval
python3 sandbox/run.py eval --env-file backend/.env --repeat 3 --label "gemini-3.8-flash"
python3 sandbox/run.py eval --scenario nginx-502 app-down --keep    # kontenery zostają do obejrzenia
```

Dla każdego scenariusza startuje świeży kontener, Pipe dostaje polecenie, a skrypt zgadza się na każdą
proponowaną zmianę (do 12 razy), jak użytkownik, który mówi „tak”. Potem uruchamia `check.sh`. Wynik
pokazuje liczbę zdanych scenariuszy, liczbę potwierdzeń i czas. Szczegóły z odpowiedziami trafiają do
`sandbox/results/*.json` (poza gitem). Modele nie są deterministyczne, więc do porównań użyj `--repeat`.

Uwaga: każdy scenariusz to kilka do kilkunastu zapytań do modelu, czyli realny koszt u płatnego providera.

## Autotest

```bash
python3 sandbox/run.py self-test
```

Bez modelu sprawdza, że każdy scenariusz po zepsuciu nie przechodzi `check.sh`, po `fix.sh` przechodzi, a Pipe
w kontenerze odpowiada. Uruchamia go CI przy każdym pushu.

Nowy scenariusz to nowy katalog w `scenarios/` z czterema plikami; `backend/tests/test_sandbox.py` pilnuje, żeby
żadnego nie brakowało.
