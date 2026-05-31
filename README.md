# MCP Session Bridge

WW-MCP to remote MCP bridge dla rozmów prowadzonych naprzemiennie z różnymi
modelami LLM. Jego rola jest teraz celowo wąska: tworzy sesje, zapisuje kolejne
wymiany rozmowy, oddaje transcript w bezpiecznych chunkach i zapisuje
podsumowania sesji do plików Markdown.

Publiczny endpoint produkcyjny:

```text
https://mcp.panchmurka.wtf/mcp
```

## Po co to jest

Kontekst merytoryczny Wojtek dostarcza ręcznie do okna rozmowy jako pliki `.md`
albo wklejony tekst. MCP nie próbuje już przesyłać tych plików do modeli, bo duże
wyniki tool calli mogą być przycinane przez klienta/model.

Typowy przepływ:

1. Model tworzy albo wybiera sesję WW-MCP.
2. Model pobiera `get_session_overview`.
3. Model pobiera wszystkie chunki transcriptu przez `get_session_transcript_chunk`.
4. Model odpowiada Wojtkowi na podstawie ręcznie dostarczonego kontekstu i
   transcriptu z MCP.
5. Przed pokazaniem odpowiedzi model zapisuje pełną wymianę przez `save_exchange`.
6. Na prośbę Wojtka model zapisuje podsumowanie przez `save_session_summary`.

## Główne funkcje

- Remote MCP przez HTTPS, wystawiony przez FastMCP i Uvicorn.
- OAuth authorization-code + PKCE dla connectorów Claude.ai i ChatGPT.
- Dynamic client registration dla klientów MCP.
- Sesje rozmowy z `session_id`, tytułem i pełnym transcript storage w SQLite.
- Chunkowany odczyt transcriptu, żeby długie rozmowy dało się pobrać przez wiele
  małych tool calli.
- Zapis pełnych wymian: wiadomość Wojtka, odpowiedź modelu, nazwa modelu i czas
  odpowiedzi w strefie `Europe/Warsaw`.
- Podsumowania sesji zapisywane jako pliki `.md` poza mechanizmem transcriptu.
- Offline viewer HTML dla zapisanych sesji.
- Panel admina do ręcznej korekty transcriptów przez przeglądarkę.

## Narzędzia MCP

| Tool | Opis |
| --- | --- |
| `bridge_ping` | Minimalny health check ścieżki MCP. |
| `auth_whoami` | Zwraca klienta OAuth przypisanego do aktualnego tokena. |
| `save_probe` / `read_probe` | Prosty zapis/odczyt testowy do sprawdzania connectorów. |
| `create_session` | Tworzy nową sesję rozmowy. |
| `list_sessions` | Listuje zapisane sesje. |
| `get_session_overview` | Zwraca lekkie metadata sesji i informację o chunkach transcriptu. |
| `get_session_transcript_chunk` | Zwraca jeden ograniczony chunk transcriptu. |
| `save_exchange` | Zapisuje jedną pełną wymianę użytkownik-model. |
| `save_session_summary` | Zapisuje podsumowanie sesji jako plik Markdown. |
| `list_session_summaries` | Listuje zapisane podsumowania sesji. |

## Chunkowanie transcriptu

`get_session_overview(session_id)` zwraca między innymi:

- `exchange_count`
- `turn_count`
- `transcript_char_count`
- `transcript_sha256`
- `transcript_chunk_count`
- `chunk_max_lines`
- `chunk_max_chars`

Model powinien potem pobrać:

```text
get_session_transcript_chunk(session_id, 1)
get_session_transcript_chunk(session_id, 2)
...
get_session_transcript_chunk(session_id, transcript_chunk_count)
```

Domyślne limity chunków:

```text
BRIDGE_TRANSCRIPT_CHUNK_MAX_LINES=180
BRIDGE_TRANSCRIPT_CHUNK_MAX_CHARS=12000
```

Każdy chunk zawiera `has_more`, `next_chunk_index`, zakres linii/znaków oraz
fragment `transcript_markdown`. Złączenie wszystkich chunków w kolejności daje
pełny transcript.

## Podsumowania sesji

`save_session_summary` zapisuje Markdown do katalogu:

```text
data/session-summaries/<session_id>/
```

Na produkcji katalog można zmienić przez:

```text
BRIDGE_SUMMARIES_DIR=/root/ww-session-summaries
```

Podsumowania nie są automatycznym kontekstem dla modeli i nie aktualizują żadnych
manifestów. Są osobnymi artefaktami, które można później ręcznie wykorzystać.

## Konfiguracja

Konfiguracja jest ładowana z `.env` w katalogu repozytorium.

Najważniejsze zmienne:

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `BRIDGE_PUBLIC_BASE_URL` | wymagane | Publiczny origin, np. `https://mcp.panchmurka.wtf`. |
| `BRIDGE_RESOURCE_PATH` | `/mcp` | Ścieżka streamable HTTP MCP. |
| `BRIDGE_DB_PATH` | `data/bridge.sqlite3` | SQLite z OAuth, sesjami i transcriptami. |
| `BRIDGE_SUMMARIES_DIR` | `data/session-summaries` | Katalog podsumowań Markdown. |
| `BRIDGE_TRANSCRIPT_CHUNK_MAX_LINES` | `180` | Maksymalna liczba linii w chunku transcriptu. |
| `BRIDGE_TRANSCRIPT_CHUNK_MAX_CHARS` | `12000` | Maksymalna liczba znaków w chunku transcriptu. |
| `BRIDGE_OWNER_USERNAME` | `wojtek` | Login właściciela w OAuth login form. |
| `BRIDGE_OWNER_PASSWORD_HASH` | wymagane | Hash hasła właściciela. |
| `BRIDGE_SECRET_KEY` | wymagane | Sekret do hashy tokenów i kodów. |
| `BRIDGE_SCOPE` | `bridge` | Wymagany scope MCP. |

Wygenerowanie albo odświeżenie lokalnego `.env`:

```bash
uv run python scripts/set_owner_password.py --username wojtek
```

Na produkcji można zapisać jednorazowy plik z hasłem:

```bash
uv run python scripts/set_owner_password.py \
  --username wojtek \
  --write-once-file secrets/owner-login.txt
```

## Development

Wymagania:

- Python 3.12+
- `uv`

Instalacja zależności:

```bash
uv sync
```

Uruchomienie lokalne:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload
```

Health check:

```bash
curl http://127.0.0.1:8787/healthz
```

Testy:

```bash
uv run pytest
```

Jeżeli lokalne `.venv` zostało przeniesione z innej maszyny i ma zepsute
shebangi, odbuduj je:

```bash
rm -rf .venv
uv sync
uv run pytest
```

## Offline Session Viewer

`session-viewer.html` renderuje zapisane sesje jako widok czatowy bez
wystawiania publicznej trasy HTTP.

Eksport danych:

```bash
uv run python scripts/session_audit.py export-viewer --output session-viewer-data.json
```

Tryb ciągłego odświeżania:

```bash
uv run python scripts/session_audit.py export-viewer --output session-viewer-data.json --watch 5
python3 -m http.server 8799 --bind 127.0.0.1
```

Na VPS-ie można użyć SSH port forwarding:

```bash
ssh -L 8799:127.0.0.1:8799 root@89.167.57.190
```

Potem otwórz:

```text
http://127.0.0.1:8799/session-viewer.html
```

Jeżeli otwierasz plik HTML bez serwera, użyj przycisku `Wczytaj JSON` i wskaż
`session-viewer-data.json`.

## Panel admina sesji

Panel korekty działa na tym samym backendzie co MCP:

```text
https://mcp.panchmurka.wtf/admin/sessions
```

Logowanie używa tych samych danych właściciela co OAuth login form:
`BRIDGE_OWNER_USERNAME` i `BRIDGE_OWNER_PASSWORD_HASH`.

Panel pozwala:

- przeglądać sesje i wszystkie wymiany, także już wyłączone z transcriptu,
- edytować nazwę modelu, wiadomość użytkownika i odpowiedź modelu,
- usuwać wymiany z aktywnego transcriptu,
- przywracać usunięte wymiany.

Usuwanie jest miękkie: rekord zostaje w SQLite z `deleted_at` i
`deleted_reason`, ale `list_exchanges`, `get_session_overview` i
`get_session_transcript_chunk` pomijają go w aktywnym transcripcie. Zmiany
admina zapisują się w `exchange_admin_events`, razem ze stanem przed i po.

## Audyt sesji z CLI

Lista sesji:

```bash
uv run python scripts/session_audit.py list
```

Transcript jako Markdown:

```bash
uv run python scripts/session_audit.py show <session_id>
```

Sekwencja mówców:

```bash
uv run python scripts/session_audit.py show <session_id> --format sequence
```

JSON:

```bash
uv run python scripts/session_audit.py show <session_id> --format json
```

## Deploy na VPS

Produkcja działa jako systemd service:

```text
/etc/systemd/system/mcp-session-bridge.service
```

Szablon jest w:

```text
deploy/mcp-session-bridge.service
```

Typowy update kodu i zależności:

```bash
cd /root/mcp-session-bridge
uv sync --frozen --no-dev
systemctl restart mcp-session-bridge
systemctl status mcp-session-bridge --no-pager -l
```

Po zmianach zależności albo gdy venv wygląda niespójnie:

```bash
cd /root/mcp-session-bridge
systemctl stop mcp-session-bridge
uv sync --frozen --no-dev --reinstall
systemctl start mcp-session-bridge
```

Caddy reverse proxy:

```text
deploy/Caddyfile.mcp-session-bridge
```

Aktywacja route po ustawieniu DNS:

```bash
cd /root/mcp-session-bridge
uv run python scripts/activate_caddy.py
```

## Bezpieczeństwo i dane

- `.env`, baza SQLite, sekrety, podsumowania i eksport viewer data są gitignored.
- Tokeny OAuth i kody autoryzacyjne są zapisywane jako hashe.
- MCP wymaga bearer tokena ze scope `bridge`.
- WW-MCP nie służy do przesyłania dużych plików kontekstowych do modeli.
- Długie transcript’y należy pobierać przez wszystkie chunki wskazane przez
  `get_session_overview`.

## Ważne pliki

| Ścieżka | Rola |
| --- | --- |
| `app/main.py` | FastMCP, routes OAuth i definicje tooli. |
| `app/admin.py` | Logowanie i API panelu admina sesji. |
| `app/oauth.py` | OAuth dynamic registration, login, token exchange i refresh. |
| `app/storage.py` | SQLite schema i zapis sesji/transcriptów/tokenów. |
| `app/session_package.py` | Render transcriptu i chunkowanie. |
| `app/session_summaries.py` | Zapis i listing plikowych podsumowań sesji. |
| `docs/project-prompt-template.md` | Prompt/protokół dla modeli podłączonych do WW-MCP. |
| `scripts/session_audit.py` | CLI do audytu sesji i eksportu viewer data. |
| `admin-viewer.html` | Webowy panel korekty transcriptów. |
| `session-viewer.html` | Offline viewer transcriptów. |
