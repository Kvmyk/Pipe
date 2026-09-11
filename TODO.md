# Propozycje Usprawnień dla Projektu "Pipe"

## Wprowadzenie

Poniższy dokument przedstawia analizę obecnego stanu projektu "Pipe" (v0.3) i zawiera sugestie dotyczące potencjalnych usprawnień. Celem tych propozycji jest zwiększenie bezpieczeństwa, poprawa jakości kodu, ułatwienie dalszego rozwoju oraz wprowadzenie najlepszych praktyk inżynierii oprogramowania. Analiza została przeprowadzona na podstawie przeglądu kodu źródłowego, konfiguracji Docker oraz dokumentacji `AGENTS.md`.

## 1. Jakość Kodu i Utrzymanie

Obecnie kod jest pisany i formatowany manualnie, co w dłuższej perspektywie może prowadzić do niespójności i utrudniać wprowadzanie nowych osób do projektu.

### Sugestie:

*   **Wprowadzenie testów automatycznych:**
    *   **Problem:** Brak jakichkolwiek testów automatycznych (`AGENTS.md` to potwierdza) sprawia, że każda zmiana w logice biznesowej (np. w `agent.py` czy `security.py`) jest ryzykowna i wymaga manualnej weryfikacji.
    *   **Rozwiązanie:** Zintegrować framework `pytest` wraz z `pytest-asyncio` do obsługi kodu asynchronicznego. Należy stworzyć zestaw testów jednostkowych dla kluczowych modułów (`executor`, `security`, `tools`) oraz testy integracyjne dla głównej pętli agenta, weryfikujące poprawność obsługi całego cyklu zapytania i odpowiedzi.

*   **Automatyczne formatowanie i linting:**
    *   **Problem:** Kod jest formatowany zgodnie z PEP 8, ale proces ten jest manualny. Może to prowadzić do drobnych różnic w stylu kodu, a także przeoczenia potencjalnych błędów.
    *   **Rozwiązanie:** Wprowadzić narzędzia `black` do automatycznego, spójnego formatowania kodu oraz `ruff` (lub `flake8`) do statycznej analizy kodu (lintingu). Pozwoli to na wczesne wykrywanie błędów, nieużywanych importów czy zmiennych i zapewni jednolity wygląd całego kodu bez wysiłku ze strony programistów.

*   **Refaktoryzacja i modularyzacja:**
    *   **Problem:** Plik `backend/core/agent.py` jest bardzo rozbudowany (ponad 800 linii) i zawiera logikę obsługi wszystkich dostępnych narzędzi (`_handle_...`). Utrudnia to nawigację i modyfikację poszczególnych funkcji.
    *   **Rozwiązanie:** Rozważyć wydzielenie logiki obsługi poszczególnych narzędzi do osobnych plików lub modułów w nowym katalogu, np. `backend/core/handlers/`. Plik `agent.py` pełniłby wtedy rolę "dyspozytora", który na podstawie nazwy narzędzia deleguje zadanie do odpowiedniego handlera. Zwiększy to czytelność i ułatwi dodawanie nowych narzędzi w przyszłości.

## 2. Bezpieczeństwo

Projekt "Pipe" operuje z bardzo wysokimi uprawnieniami, co stanowi znaczące ryzyko bezpieczeństwa. Konfiguracja `docker-compose.yml` jest tego najlepszym dowodem.

### Sugestie:

*   **Ograniczenie dostępu do systemu plików (Zasada Najmniejszych Uprawnień):**
    *   **Problem:** Montowanie całego głównego systemu plików hosta (`/:/hostfs`) daje agentowi nieograniczony dostęp do każdego pliku na serwerze. Przypadkowy lub złośliwie spreparowany prompt może doprowadzić do usunięcia krytycznych danych systemowych.
    *   **Rozwiązanie:** Zamiast montować cały system plików, należy stworzyć na hoście dedykowany katalog roboczy (np. `/opt/pipe_workspace`) i montować tylko ten katalog do kontenera (`/opt/pipe_workspace:/hostfs`). Domyślnie agent powinien móc operować tylko w tej przestrzeni. Dostęp do plików poza tym katalogiem powinien być zablokowany lub wymagać dodatkowego, jawnego potwierdzenia od użytkownika na poziomie interfejsu.

*   **Zarządzanie dostępem do Docker Socket:**
    *   **Problem:** Podobnie jak w przypadku systemu plików, montowanie `docker.sock` daje agentowi pełną kontrolę nad demonem Dockera na hoście, włącznie z możliwością tworzenia, usuwania i modyfikowania dowolnych kontenerów.
    *   **Rozwiązanie:** Należy znacząco rozbudować i uszczegółowić reguły w `security.py` dotyczące komend `docker`. Zamiast ogólnych wzorców, warto stworzyć precyzyjną listę dozwolonych i zakazanych operacji, aby uniemożliwić np. zatrzymanie krytycznych usług.

*   **Uwierzytelnianie i autoryzacja:**
    *   **Problem:** Obecny mechanizm autoryzacji opiera się na pojedynczym, opcjonalnym tokenie (`AGENT_TOKEN`). Nie ma rozróżnienia na użytkowników ani poziomy uprawnień.
    *   **Rozwiązanie:** Wprowadzić system unikalnych tokenów dla każdego klienta/użytkownika. Pozwoli to na łatwe unieważnianie dostępu dla konkretnego klienta oraz, w przyszłości, na budowę systemu ról i uprawnień (np. użytkownik "admin" może restartować kontenery, a "viewer" może tylko odczytywać logi).

## 3. Architektura i Konfiguracja

Można wprowadzić kilka usprawnień, które ułatwią zarządzanie konfiguracją i zwiększą odporność aplikacji na błędy.

### Sugestie:

*   **Zarządzanie konfiguracją za pomocą Pydantic:**
    *   **Problem:** Plik `backend/config/settings.py` wczytuje zmienne środowiskowe przy użyciu `os.getenv` i manualnie konwertuje typy. Walidacja jest bardzo podstawowa.
    *   **Rozwiązanie:** Zastosować bibliotekę `Pydantic` do zarządzania ustawieniami. Stworzenie klasy dziedziczącej po `BaseSettings` pozwoli na automatyczne wczytywanie zmiennych, walidację typów (np. `int`, `str`), a także definiowanie wartości domyślnych w jednym, spójnym miejscu. Upraszcza to kod i czyni go bardziej odpornym na błędy konfiguracyjne.

*   **Bardziej szczegółowa obsługa błędów:**
    *   **Problem:** W głównym handlerze połączenia (`backend/server.py:handle_client`) znajduje się ogólny blok `except Exception`, który łapie wszystkie błędy. Utrudnia to diagnozowanie problemów.
    *   **Rozwiązanie:** Należy rozróżnić typy potencjalnych wyjątków (np. `ConnectionResetError`, `json.JSONDecodeError`, błędy operacji na plikach) i obsługiwać je w dedykowanych blokach `except`. Pozwoli to na zwracanie bardziej precyzyjnych komunikatów błędów do klienta i lepsze logowanie po stronie serwera.

## 4. CI/CD (Continuous Integration / Continuous Deployment)

Automatyzacja procesów budowania, testowania i wdrażania jest kluczowa dla szybkiego i bezpiecznego rozwoju projektu.

### Sugestie:

*   **Wprowadzenie przepływu pracy z GitHub Actions:**
    *   **Problem:** `AGENTS.md` wskazuje na brak jakiejkolwiek automatyzacji CI/CD. Każda zmiana musi być testowana i wdrażana manualnie.
    *   **Rozwiązanie:** Stworzyć prosty workflow w GitHub Actions (`.github/workflows/ci.yml`), który będzie uruchamiany dla każdego `push` i `pull request`. Workflow powinien co najmniej:
        1.  Instalować zależności projektu.
        2.  Uruchamiać linter (`ruff`) i formater w trybie sprawdzania (`black --check`).
        3.  Uruchamiać zestaw testów automatycznych (`pytest`).
    Dzięki temu każda propozycja zmiany będzie automatycznie weryfikowana, co znacząco podniesie jakość i stabilność kodu.

## Podsumowanie

Projekt "Pipe" ma solidne fundamenty i duży potencjał. Wprowadzenie powyższych usprawnień, ze szczególnym naciskiem na **bezpieczeństwo** i **automatyzację testów**, pozwoli na jego dalszy, dynamiczny rozwój w sposób bardziej kontrolowany i przewidywalny. Rekomenduje się rozpoczęcie prac od kwestii o najwyższym priorytecie: ograniczenia dostępu do systemu plików hosta oraz wprowadzenia podstawowego zestawu testów.
