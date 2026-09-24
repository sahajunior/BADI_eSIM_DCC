from app.auth.passwords import hash_password, verify_password


def test_hash_password_uses_argon2id_and_verifies() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert password_hash.startswith("$argon2id$")
    assert "correct horse battery staple" not in password_hash
    assert verify_password("correct horse battery staple", password_hash)


def test_verify_password_rejects_wrong_password_and_invalid_hash() -> None:
    password_hash = hash_password("swordfish")

    assert not verify_password("wrong", password_hash)
    assert not verify_password("swordfish", "not-a-real-hash")
