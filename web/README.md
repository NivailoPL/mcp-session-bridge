# web/

The pages `app/admin.py` serves to a logged-in owner. Nothing here is a build
artifact — the files are shipped and read from disk as they are.

| | |
| --- | --- |
| `admin-viewer.html` | The admin UI: sessions, transcripts, files, groups, settings. One file, JavaScript inline. |
| `admin-confirmation.css` `admin-confirmation.js` | The shared confirm-before-destructive-action dialog. |
| `pearl-gradient-nav.css` `pearl-gradient-nav.js` | The shared workspace navigation. |

Icons and the wordmark come from `brand/`, the PDF renderer from
`vendor/pdfjs/`; both are resolved from the repository root, not from here.

The viewer's inline JavaScript is tested by lifting single functions out of the
file and running them under Node — see `tests/viewer_harness.py`.

For an admin UI filled with realistic content, run `scripts/demo_ui.py`.
