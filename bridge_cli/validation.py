from __future__ import annotations

import re


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_domain(value: str) -> str:
    domain = value.strip().lower()
    labels = domain.split(".")
    if (
        not domain
        or len(domain) > 253
        or len(labels) < 2
        or any(not _DNS_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError("Provide a hostname such as bridge.example.com.")
    return domain


def validate_owner_updates(username: str | None, password: str | None) -> None:
    if username is not None and not username.strip():
        raise ValueError("Owner username cannot be empty.")
    if password is not None and len(password) < 10:
        raise ValueError("Owner password must contain at least 10 characters.")
