# MCP Session Bridge — paczka marki (5D)

Wersja: Przęsło v3 · kafel indygo · Instrument Sans
Data: 2026-08-08

## Znak

Most o dziewięciu pionach stojących na jednej linii (pomost) z dwoma pylonami,
które nieznacznie wystają poniżej pomostu. Skrajne belki są najniższe — most
zaczyna się płasko przy brzegu. Geometria: viewBox `10 16 100 47`.

**Nie wolno:** dodawać łuku/liny, obracać, pochylać, zmieniać rytmu belek,
rozciągać niejednorodnie, nakładać cieni ani gradientów na sam znak.

**Pole ochronne:** minimum wysokość pylonu (35 j. = 0,74 wysokości znaku) z każdej strony.
**Minimalna wielkość:** kafel 16 px, sam znak 20 px szerokości.

## Kolory

| Nazwa    | Hex     | Zastosowanie |
|----------|---------|--------------|
| Noc      | #0A0B0F | tło aplikacji |
| Panel    | #15161D | karty, panele |
| Indygo   | #6C5CF2 | kolor marki, kafel, akcenty |
| Głębia   | #4A42C4 | indygo na jasnym tle, hover |
| Lawenda  | #B3A9FF | pylony, akcent na ciemnym |
| Lawenda 2| #C9C2FF | pylony wewnątrz kafla indygo |
| Mgła     | #ECEDF2 | tekst podstawowy, znak w kaflu |

Piktogramy grup pozostają wielobarwne — znak zawsze trzyma indygo i nie konkuruje z żadną grupą.

## Typografia

- **Instrument Sans** — wordmark, nagłówki, interfejs. Wordmark: 600, letter-spacing −0.6 do −0.7.
- **IBM Plex Mono** — „MCP", identyfikatory sesji, timestampy, etykiety wersalikowe (500, tracking 2).

Google Fonts:
```
https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap
```

## Zawartość paczki

### brand/svg/
- `mark-indigo`, `mark-mist`, `mark-deep` — sam znak (indygo / jasny / na jasnym tle)
- `mark-mono-black`, `mark-mono-white` — jednokolorowe, do druku i nadruków
- `tile-indigo`, `tile-deep`, `tile-night`, `tile-mist` — kafel 128×128, promień 32 (25%)
- `lockup-horizontal-dark|light` — znak + wordmark bez kafla
- `lockup-horizontal-tile-dark|light` — kafel + wordmark (wersja aplikacyjna)
- `lockup-vertical-dark|light` — układ pionowy

### brand/png/
- `icon-indigo-16…1024` — favicon, PWA, apple-touch (180), sklepy (512/1024)
- `icon-night-512|1024`, `icon-mist-512|1024` — warianty tła
- `mark-*-1200` — sam znak, przezroczyste tło

### brand/tokens.json
Kolory i typografia w formacie do importu.

## Uwaga o lockupach SVG

Teksty w plikach lockupów są żywym tekstem (`<text>`), nie krzywymi — renderują się
poprawnie wszędzie tam, gdzie Instrument Sans i IBM Plex Mono są dostępne (web, Figma
z zainstalowanymi fontami). Do druku lub przekazania na zewnątrz zamień teksty na krzywe
(Illustrator/Figma: Outline text) albo użyj samego znaku + złożenia tekstu na miejscu.
