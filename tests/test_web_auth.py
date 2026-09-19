"""Password hash scheme for the admin panel (review P0-8).

The scheme is stdlib-only PBKDF2 so that a hash created on one install can
always be verified on another. The legacy ``sha256$salt$hex`` format is the
only one that exists in deployed configs today and stays verifiable; bcrypt
hashes are verified when the module happens to be present but are never
created.
"""

import hashlib

import pytest
from pydantic import TypeAdapter, ValidationError

from mercure_gateway.config import GatewayConfig, WebUIConfig
from mercure_gateway.web.auth import hash_password, verify_password


def test_pbkdf2_roundtrip_verifies() -> None:
    h = hash_password("s3cret")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)


def test_pbkdf2_salt_is_per_attempt() -> None:
    """Two hashes of the same password differ — no rainbow-table reuse."""
    assert hash_password("s3cret") != hash_password("s3cret")


def test_pbkdf2_iterations_are_read_from_the_hash() -> None:
    """An old hash stays verifiable after the default iteration count is raised."""
    h = hash_password("s3cret", iterations=1000)
    assert h.split("$")[1] == "1000"
    assert verify_password("s3cret", h)


def test_pbkdf2_garbage_returns_false_instead_of_raising() -> None:
    assert not verify_password("s3cret", "pbkdf2$notanumber$zzz$ww")
    assert not verify_password("s3cret", "pbkdf2$1000$nothex$deadbeef")


def test_legacy_sha256_hash_still_verifies() -> None:
    """The only format present in deployed configs today must keep working."""
    salt = "somesalt"
    expected = hashlib.sha256(f"{salt}s3cret".encode()).hexdigest()
    assert verify_password("s3cret", f"sha256${salt}${expected}")
    assert not verify_password("wrong", f"sha256${salt}${expected}")


def test_unrecognised_hash_returns_false() -> None:
    assert not verify_password("s3cret", "HASH")
    assert not verify_password("s3cret", "")


def test_bcrypt_hash_returns_false_without_the_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unverifiable hash must not become an unhandled 500 on the login path."""
    import builtins

    real_import = builtins.__import__

    def _no_bcrypt(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "bcrypt":
            raise ImportError("no bcrypt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_bcrypt)
    assert not verify_password("s3cret", "$2b$12$abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ")


@pytest.mark.parametrize(
    "value",
    [
        "pbkdf2$200000$00112233445566778899aabbccddeeff$00112233445566778899aabbccddeeff",
        "sha256$salt$hash",
        "$2b$12$abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ",
        "",
    ],
)
def test_hash_format_is_accepted_by_the_config(value: str) -> None:
    adapter: TypeAdapter[WebUIConfig] = TypeAdapter(WebUIConfig)
    assert adapter.validate_python({"auth_password_hash": value}).auth_password_hash == value


@pytest.mark.parametrize("value", ["HASH", "md5$abc$def", "sha256-only-two-parts"])
def test_unrecognised_hash_is_rejected_by_the_config(value: str) -> None:
    adapter: TypeAdapter[WebUIConfig] = TypeAdapter(WebUIConfig)
    with pytest.raises(ValidationError) as exc:
        adapter.validate_python({"auth_password_hash": value})
    assert "auth_password_hash" in str(exc.value)


def test_auth_enabled_requires_a_hash() -> None:
    """Makes the permanent-lockout combination unreachable at the schema level."""
    adapter: TypeAdapter[WebUIConfig] = TypeAdapter(WebUIConfig)
    with pytest.raises(ValidationError) as exc:
        adapter.validate_python({"auth_enabled": True, "auth_password_hash": ""})
    assert "auth_password_hash" in str(exc.value)


def test_auth_enabled_accepts_a_real_hash() -> None:
    adapter: TypeAdapter[WebUIConfig] = TypeAdapter(WebUIConfig)
    ui = adapter.validate_python(
        {"auth_enabled": True, "auth_password_hash": hash_password("s3cret")}
    )
    assert ui.auth_enabled


def test_garbage_hash_is_rejected_at_the_top_level_config() -> None:
    """A config file with a bogus hash cannot boot, rather than locking out silently."""
    with pytest.raises(ValidationError) as exc:
        GatewayConfig.model_validate(
            {"web_ui": {"auth_enabled": True, "auth_password_hash": "HASH"}}
        )
    assert "auth_password_hash" in str(exc.value)
