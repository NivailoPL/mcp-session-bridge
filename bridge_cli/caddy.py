from __future__ import annotations

import re

# HSTS for this host only: no includeSubDomains, so other sites on the same domain are unaffected.
SECURITY_HEADERS = """    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        -Server
    }
"""


def bridge_site_block(domain: str) -> str:
    return f"{domain} {{\n    encode zstd gzip\n{SECURITY_HEADERS}    reverse_proxy 127.0.0.1:8787\n}}\n"


def has_site(caddyfile: str, domain: str) -> bool:
    pattern = _domain_pattern(domain)
    depth = 0
    for raw in caddyfile.splitlines():
        header = _site_header(raw) if depth == 0 else None
        if header is not None and pattern.search(header):
            return True
        depth += _brace_delta(raw)
    return False


def replace_site_address(caddyfile: str, old_domain: str | None, new_domain: str) -> str:
    if not old_domain:
        return caddyfile
    pattern = _domain_pattern(old_domain)
    rendered: list[str] = []
    changed = False
    depth = 0
    for raw in caddyfile.splitlines(keepends=True):
        header = _site_header(raw) if depth == 0 else None
        if not changed and header is not None and pattern.search(header):
            rendered.append(pattern.sub(new_domain, raw, count=1))
            changed = True
        else:
            rendered.append(raw)
        depth += _brace_delta(raw)
    return "".join(rendered)


def _site_header(raw: str) -> str | None:
    stripped = raw.strip()
    if stripped.startswith("#") or not stripped.endswith("{"):
        return None
    return stripped[:-1].strip()


def _domain_pattern(domain: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9.-])(?:https?://)?{re.escape(domain)}(?![A-Za-z0-9.-])")


def _brace_delta(raw: str) -> int:
    code = raw.split("#", 1)[0]
    return code.count("{") - code.count("}")
