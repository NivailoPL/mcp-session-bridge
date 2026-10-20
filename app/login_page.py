"""One look for every page that asks for the owner password.

The admin login and the OAuth connector sign-in share this shell: the brand
panel on the left, a form panel on the right. Callers pass the right-hand panel
as HTML they have already escaped.
"""

from __future__ import annotations

import html

from starlette.responses import HTMLResponse

from app.settings import ROOT

LOCKUP_PATH = ROOT / "brand" / "svg" / "lockup-horizontal-dark.svg"
FALLBACK_WORDMARK = '<span class="fallback-wordmark"><span>MCP</span> Session Bridge</span>'
# The page carries no script; framing is refused so a sign-in cannot be overlaid.
LOGIN_PAGE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src data:; base-uri 'none'; frame-ancestors 'none'"
)
ARROW_ICON = (
    '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 8h9m-4-4 4 4-4 4" '
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def brand_lockup_svg() -> str:
    try:
        return LOCKUP_PATH.read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_WORDMARK


def alert_html(message: str | None) -> str:
    if not message:
        return ""
    return (
        '<div class="login-alert" role="alert">'
        '<span class="alert-dot" aria-hidden="true"></span>'
        f"<span>{html.escape(message)}</span>"
        "</div>"
    )


def render_login_page(
    *,
    page_title: str,
    visual_eyebrow: str,
    visual_title: str,
    visual_copy: str,
    meta_label: str,
    meta_text: str,
    panel_html: str,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    compact_on_mobile: bool = False,
) -> HTMLResponse:
    # On a phone a task page (connector consent) shows the logo as a header so the form is in view.
    shell_class = "login-shell is-task" if compact_on_mobile else "login-shell"
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>{html.escape(page_title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>{LOGIN_PAGE_STYLE}</style>
</head>
<body>
  <main class="{shell_class}">
    <section class="login-visual" aria-label="MCP Session Bridge">
      <div>
        <div class="login-logo">{brand_lockup_svg()}</div>
        <p class="eyebrow">{html.escape(visual_eyebrow)}</p>
        <h1>{html.escape(visual_title)}</h1>
        <p class="visual-copy">{html.escape(visual_copy)}</p>
      </div>
      <div class="visual-bottom">
        <span class="signal-mark" aria-hidden="true"></span>
        <span class="visual-meta"><span class="mono">{html.escape(meta_label)}</span><span>{html.escape(meta_text)}</span></span>
      </div>
    </section>
    <section class="login-form-panel" aria-labelledby="login-title">
      <div class="login-form-wrap">
{panel_html}
      </div>
    </section>
  </main>
</body>
</html>"""
    return HTMLResponse(
        body,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": LOGIN_PAGE_CSP,
            "X-Content-Type-Options": "nosniff",
            **(headers or {}),
        },
    )


LOGIN_PAGE_STYLE = """
    :root {
      color-scheme: dark;
      --bg-base: #0a0b0f;
      --panel: #15161d;
      --text: #ecedf2;
      --muted: #8e93a6;
      --faint: #6e7387;
      --stroke: rgba(255, 255, 255, .1);
      --stroke-soft: rgba(255, 255, 255, .065);
      --accent: #6c5cf2;
      --accent-hover: #7c70f5;
      --accent-text: #b3a9ff;
      --accent-soft: rgba(108, 92, 242, .16);
      --danger: #fb7185;
      --danger-soft: rgba(251, 113, 133, .12);
      --ok: #34d399;
      --warn: #fbbf24;
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; margin: 0; }
    body {
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 1.5rem;
      overflow-x: hidden;
      background:
        radial-gradient(58rem 38rem at 8% -8%, rgba(108, 92, 242, .2), transparent 66%),
        radial-gradient(42rem 30rem at 105% 108%, rgba(74, 66, 196, .16), transparent 68%),
        var(--bg-base);
      color: var(--text);
      font-family: Manrope, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: .26;
      background-image: linear-gradient(rgba(255, 255, 255, .025) 1px, transparent 1px), linear-gradient(90deg, rgba(255, 255, 255, .025) 1px, transparent 1px);
      background-size: 48px 48px;
      mask-image: linear-gradient(to bottom, black, transparent 78%);
    }
    button, input { font: inherit; color: inherit; }
    button:focus-visible, input:focus-visible, a:focus-visible { outline: 2px solid var(--accent-hover); outline-offset: 3px; }
    .login-shell {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: minmax(0, 1.08fr) minmax(20rem, .92fr);
      width: min(100%, 68rem);
      min-height: min(36rem, calc(100vh - 3rem));
      overflow: hidden;
      border: 1px solid var(--stroke);
      border-radius: 22px;
      background: rgba(21, 22, 29, .86);
      box-shadow: 0 32px 90px rgba(0, 0, 0, .55), 0 0 0 1px rgba(108, 92, 242, .035);
      backdrop-filter: blur(20px);
    }
    .login-visual {
      position: relative;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-width: 0;
      padding: clamp(2.2rem, 5vw, 4.5rem);
      overflow: hidden;
      border-right: 1px solid var(--stroke-soft);
      background:
        radial-gradient(28rem 22rem at 10% 6%, rgba(108, 92, 242, .17), transparent 72%),
        linear-gradient(145deg, rgba(255, 255, 255, .035), transparent 58%);
    }
    .login-visual::after {
      content: "";
      position: absolute;
      right: -9rem;
      bottom: -12rem;
      width: 26rem;
      height: 26rem;
      border: 1px solid rgba(179, 169, 255, .12);
      border-radius: 50%;
      box-shadow: 0 0 0 3.2rem rgba(179, 169, 255, .025), 0 0 0 6.4rem rgba(179, 169, 255, .018);
      pointer-events: none;
    }
    .login-logo { position: relative; z-index: 1; width: min(100%, 27rem); margin-bottom: clamp(4rem, 11vh, 8rem); }
    .login-logo svg { display: block; width: 100%; height: auto; }
    .fallback-wordmark { display: block; color: var(--text); font-size: 1.5rem; font-weight: 700; letter-spacing: -.04em; }
    .fallback-wordmark span { margin-right: .45rem; color: var(--accent-text); font-family: "Geist Mono", ui-monospace, monospace; font-size: .78em; letter-spacing: .12em; }
    .eyebrow { margin: 0 0 1rem; color: var(--accent-text); font-family: "Geist Mono", ui-monospace, monospace; font-size: .68rem; font-weight: 500; letter-spacing: .18em; text-transform: uppercase; }
    h1, h2 { letter-spacing: -.055em; }
    h1 { max-width: 28rem; margin: 0 0 1rem; font-size: clamp(2.35rem, 5vw, 4rem); line-height: .98; }
    .visual-copy { max-width: 30rem; margin: 0; color: var(--muted); font-size: 1rem; line-height: 1.75; }
    .visual-bottom { position: relative; z-index: 1; display: flex; align-items: center; gap: .85rem; margin-top: 3rem; color: var(--faint); }
    .signal-mark { display: grid; place-items: center; width: 2.5rem; height: 2.5rem; flex: 0 0 auto; border: 1px solid rgba(179, 169, 255, .3); border-radius: 10px; background: var(--accent); box-shadow: 0 10px 24px rgba(108, 92, 242, .23); }
    .signal-mark::before { content: ""; width: 1.05rem; height: 1.05rem; border: 2px solid #ecedf2; border-radius: 50%; box-shadow: 0 0 0 4px rgba(201, 194, 255, .45); }
    .mono { font-family: "Geist Mono", ui-monospace, SFMono-Regular, Menlo, monospace; }
    .visual-meta { display: grid; gap: .2rem; }
    .visual-meta .mono { color: var(--accent-text); font-size: .65rem; letter-spacing: .13em; }
    .visual-meta span:last-child { font-size: .74rem; }
    .login-form-panel { display: grid; place-items: center; padding: clamp(2rem, 5vw, 4rem); background: rgba(10, 11, 15, .22); }
    .login-form-wrap { width: min(100%, 22rem); }
    .form-header { margin-bottom: 2rem; }
    .form-header h2 { margin: 0 0 .65rem; font-size: clamp(1.8rem, 3vw, 2.25rem); line-height: 1; }
    .form-header p:last-child { margin: 0; color: var(--muted); font-size: .9rem; line-height: 1.6; }
    .login-alert { display: flex; align-items: flex-start; gap: .65rem; margin: 0 0 1.25rem; padding: .8rem .9rem; border: 1px solid rgba(251, 113, 133, .28); border-radius: 10px; background: var(--danger-soft); color: #fecdd3; font-size: .82rem; line-height: 1.45; }
    .alert-dot { width: .5rem; height: .5rem; flex: 0 0 auto; margin-top: .32rem; border-radius: 50%; background: var(--danger); box-shadow: 0 0 0 4px rgba(251, 113, 133, .12); }
    .login-form { display: grid; gap: 1rem; }
    .field { display: grid; gap: .45rem; }
    label { color: #d6d8e2; font-size: .78rem; font-weight: 600; letter-spacing: .01em; }
    input { width: 100%; min-height: 3rem; padding: .75rem .85rem; border: 1px solid var(--stroke); border-radius: 10px; background: rgba(0, 0, 0, .28); transition: border-color .16s ease, box-shadow .16s ease, background .16s ease; }
    input::placeholder { color: var(--faint); }
    input:hover { border-color: rgba(255, 255, 255, .16); }
    input:focus { outline: none; border-color: var(--accent); background: rgba(0, 0, 0, .38); box-shadow: 0 0 0 3px var(--accent-soft); }
    input:-webkit-autofill,
    input:-webkit-autofill:hover,
    input:-webkit-autofill:focus,
    input:-webkit-autofill:active {
      -webkit-text-fill-color: var(--text);
      caret-color: var(--text);
      background-color: #1d1e27 !important;
      -webkit-box-shadow: 0 0 0 1000px #1d1e27 inset;
      box-shadow: 0 0 0 1000px #1d1e27 inset;
      border-color: rgba(179, 169, 255, .36);
      transition: background-color 9999s ease-out 0s;
    }
    input:-moz-autofill {
      -webkit-text-fill-color: var(--text);
      caret-color: var(--text);
      background-color: #1d1e27 !important;
      -webkit-box-shadow: 0 0 0 1000px #1d1e27 inset;
      box-shadow: 0 0 0 1000px #1d1e27 inset;
      border-color: rgba(179, 169, 255, .36);
      transition: background-color 9999s ease-out 0s;
    }
    input:autofill {
      -webkit-text-fill-color: var(--text);
      caret-color: var(--text);
      background-color: #1d1e27 !important;
      -webkit-box-shadow: 0 0 0 1000px #1d1e27 inset;
      box-shadow: 0 0 0 1000px #1d1e27 inset;
      border-color: rgba(179, 169, 255, .36);
      transition: background-color 9999s ease-out 0s;
    }
    input:-webkit-autofill:focus {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    input:-moz-autofill:focus {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    input:autofill:focus {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    .login-submit { display: flex; align-items: center; justify-content: space-between; width: 100%; min-height: 3.1rem; margin-top: .35rem; padding: .75rem .9rem .75rem 1rem; border: 1px solid rgba(179, 169, 255, .24); border-radius: 10px; background: var(--accent); color: #fff; font-size: .86rem; font-weight: 700; text-decoration: none; cursor: pointer; box-shadow: 0 12px 25px rgba(108, 92, 242, .2); transition: background .16s ease, transform .16s ease, box-shadow .16s ease; }
    .login-submit:hover { background: var(--accent-hover); box-shadow: 0 15px 30px rgba(108, 92, 242, .3); transform: translateY(-1px); }
    .login-submit:active { transform: translateY(0); }
    .login-submit svg { width: 1rem; height: 1rem; }
    .login-secondary { display: flex; align-items: center; justify-content: center; width: 100%; min-height: 2.8rem; padding: .65rem 1rem; border: 1px solid var(--stroke); border-radius: 10px; background: transparent; color: var(--text); font-size: .82rem; font-weight: 600; text-decoration: none; cursor: pointer; transition: border-color .16s ease, background .16s ease; }
    .login-secondary:hover { border-color: rgba(255, 255, 255, .2); background: rgba(255, 255, 255, .04); }
    .login-actions { display: grid; gap: .65rem; }
    .login-footer { display: flex; align-items: center; gap: .5rem; margin: 1.7rem 0 0; color: var(--faint); font-size: .7rem; line-height: 1.5; }
    .status-dot { width: .42rem; height: .42rem; flex: 0 0 auto; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 4px rgba(52, 211, 153, .1); }
    .status-dot.is-warn { background: var(--warn); box-shadow: 0 0 0 4px rgba(251, 191, 36, .12); }
    .consent { display: grid; gap: .55rem; margin: 0 0 1.4rem; padding: .95rem 1rem; border: 1px solid var(--stroke); border-radius: 12px; background: rgba(0, 0, 0, .22); }
    .consent-label { color: var(--faint); font-family: "Geist Mono", ui-monospace, monospace; font-size: .62rem; letter-spacing: .14em; text-transform: uppercase; }
    .consent-host { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem .6rem; }
    .consent-host strong { overflow-wrap: anywhere; font-family: "Geist Mono", ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 1.05rem; font-weight: 500; letter-spacing: -.01em; }
    .consent-badge { display: inline-flex; align-items: center; gap: .35rem; padding: .16rem .5rem; border: 1px solid var(--stroke); border-radius: 999px; color: var(--muted); font-size: .66rem; font-weight: 700; letter-spacing: .02em; white-space: nowrap; }
    .consent-badge::before { content: ""; width: .38rem; height: .38rem; border-radius: 50%; background: currentColor; }
    .consent-badge[data-tone="new"] { color: var(--warn); border-color: rgba(251, 191, 36, .3); background: rgba(251, 191, 36, .1); }
    .consent-badge[data-tone="known"] { color: var(--ok); border-color: rgba(52, 211, 153, .28); background: rgba(52, 211, 153, .1); }
    .consent-badge[data-tone="local"] { color: var(--accent-text); border-color: rgba(179, 169, 255, .3); background: var(--accent-soft); }
    .consent-name { margin: 0; color: var(--muted); font-size: .8rem; line-height: 1.5; overflow-wrap: anywhere; }
    .login-note { margin: 0 0 1.4rem; color: var(--muted); font-size: .86rem; line-height: 1.6; }
    .login-note strong { color: var(--text); font-weight: 600; }
    @media (max-width: 760px) {
      body { padding: .8rem; }
      .login-shell { grid-template-columns: 1fr; min-height: auto; border-radius: 17px; }
      .login-visual { min-height: 23rem; padding: 2.1rem; border-right: 0; border-bottom: 1px solid var(--stroke-soft); }
      .login-logo { width: min(100%, 23rem); margin-bottom: 4rem; }
      h1 { font-size: clamp(2.25rem, 12vw, 3.2rem); }
      .login-form-panel { padding: 2.1rem; }
    }
    @media (max-width: 760px) {
      .login-shell.is-task .login-visual { min-height: 0; padding: 1.5rem 2.1rem; }
      .login-shell.is-task .login-logo { width: min(100%, 15rem); margin-bottom: 0; }
      .login-shell.is-task .login-visual > div > :not(.login-logo),
      .login-shell.is-task .visual-bottom { display: none; }
    }
    @media (max-width: 390px) {
      .login-visual, .login-form-panel { padding: 1.5rem; }
      .login-visual { min-height: 21rem; }
    }
"""
