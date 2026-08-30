# MCP Session Bridge — Brand Package (5D)

Version: Span v3 · indigo tile · Instrument Sans
Date: 2026-08-08

## The Mark

A bridge of nine verticals standing on a single line (the deck) with two pylons
that extend slightly below it. The outermost beams are the shortest — the bridge
begins flat at the bank. Geometry: viewBox `10 16 100 47`.

**Never:** add an arch or cable, rotate, skew, change the rhythm of the beams,
scale non-uniformly, or apply shadows or gradients to the mark itself.

**Clear space:** at least one pylon height (35 units = 0.74 of the mark's height)
on every side.
**Minimum size:** 16 px for the tile, 20 px wide for the bare mark.

## Colors

| Name      | Hex     | Use |
|-----------|---------|-----|
| Night     | #0A0B0F | application background |
| Panel     | #15161D | cards, panels |
| Indigo    | #6C5CF2 | brand color, tile, accents |
| Deep      | #4A42C4 | indigo on light backgrounds, hover |
| Lavender  | #B3A9FF | pylons, accent on dark |
| Lavender 2| #C9C2FF | pylons inside the indigo tile |
| Mist      | #ECEDF2 | body text, the mark inside the tile |

Group pictograms stay multicolored — the mark always holds indigo and never
competes with a group.

## Typography

- **Instrument Sans** — wordmark, headings, interface. Wordmark: 600,
  letter-spacing −0.6 to −0.7.
- **IBM Plex Mono** — "MCP", session identifiers, timestamps, uppercase labels
  (500, tracking 2).

Google Fonts:

```
https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap
```

## What Is In The Package

### brand/svg/
- `mark-indigo`, `mark-mist`, `mark-deep` — the bare mark (indigo / light / for light backgrounds)
- `mark-mono-black`, `mark-mono-white` — single color, for print and merchandise
- `tile-indigo`, `tile-deep`, `tile-night`, `tile-mist` — 128×128 tile, radius 32 (25%)
- `lockup-horizontal-dark|light` — mark plus wordmark, no tile
- `lockup-horizontal-tile-dark|light` — tile plus wordmark (the application version)
- `lockup-vertical-dark|light` — vertical arrangement

In every `dark`/`light` pair, `dark` is the variant for dark backgrounds.

### brand/png/
- `icon-indigo-16…1024` — favicon, PWA, apple-touch (180), stores (512/1024)
- `icon-night-512|1024`, `icon-mist-512|1024` — background variants
- `mark-*-1200` — the bare mark, transparent background

### brand/tokens.json
Colors and typography in an importable format.

## A Note On The SVG Lockups

Text in the lockup files is live text (`<text>`), not outlines. It renders
correctly wherever Instrument Sans and IBM Plex Mono are available — the web,
or Figma with the fonts installed. For print, or before handing a file to
someone outside the project, convert the text to outlines (Illustrator or
Figma: Outline text), or use the bare mark and set the text in place.

## Where These Are Used

The admin UI loads icons and lockups from here through `/admin/assets/brand/...`
(see [web/README.md](../web/README.md)). The repository README uses the vertical
lockups directly.
