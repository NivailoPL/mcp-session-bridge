from app.security import password_hash, pkce_s256, verify_password


def test_password_hash_round_trip() -> None:
    stored = password_hash("not-a-real-password")

    assert verify_password("not-a-real-password", stored)
    assert not verify_password("wrong-password", stored)


def test_pkce_s256_known_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    assert pkce_s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
