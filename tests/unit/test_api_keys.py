from control_plane.infrastructure.auth.api_keys import (
    extract_prefix,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)


def test_generated_key_roundtrip() -> None:
    generated = generate_api_key()
    assert generated.full_key.startswith("cp_")
    assert extract_prefix(generated.full_key) == generated.prefix
    assert verify_api_key(generated.full_key, generated.key_hash)


def test_keys_are_unique() -> None:
    a, b = generate_api_key(), generate_api_key()
    assert a.full_key != b.full_key
    assert a.prefix != b.prefix


def test_hash_is_stable_and_not_plaintext() -> None:
    generated = generate_api_key()
    assert hash_api_key(generated.full_key) == generated.key_hash
    assert generated.full_key not in generated.key_hash


def test_extract_prefix_rejects_malformed() -> None:
    assert extract_prefix("") is None
    assert extract_prefix("not-a-key") is None
    assert extract_prefix("cp_short") is None
    assert extract_prefix("xx_12345678_secret") is None
    assert extract_prefix("cp_1234567_secret") is None  # prefix of wrong length


def test_verify_rejects_wrong_key() -> None:
    a, b = generate_api_key(), generate_api_key()
    assert not verify_api_key(a.full_key, b.key_hash)


def test_break_glass_prefix_cannot_collide_with_an_ordinary_one() -> None:
    from control_plane.infrastructure.auth.api_keys import (
        generate_break_glass_key,
        is_break_glass_prefix,
    )

    emergency = generate_break_glass_key()
    assert extract_prefix(emergency.full_key) == emergency.prefix
    assert is_break_glass_prefix(emergency.prefix)
    # An ordinary prefix is hex; "g" never occurs in it.
    assert not any(is_break_glass_prefix(generate_api_key().prefix) for _ in range(200))
    assert not is_break_glass_prefix(None)
