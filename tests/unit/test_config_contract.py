"""The `.env.example` <-> `Settings` contract (master prompt §3.4).

Every variable in `.env.example` must be read by `Settings`, and every setting in `Settings`
must appear in `.env.example`. This test is the enforcement mechanism — without it the two
drift apart silently and a production deploy misses a variable it needed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.config import DEFAULT_PRICE_TABLE, ROLE_CLEARANCE, Settings
from app.enums import Classification, Role

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

# A dummy secret that is long, obviously fake, and — crucially — does not begin with any of
# the placeholder prefixes the fail-fast validator rejects (`CHANGE_ME`, `xxx`, ...).
DUMMY_SECRET = "d4nah-test-secret-9f2c1a7e5b8d3406-not-a-real-key"

# Every environment variable Settings reads. Cleared before constructing a Settings instance
# so the test asserts against declared defaults, not against whatever is in the developer's
# shell or in conftest.
_ENV_VARS = [name.upper() for name in Settings.model_fields]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every DANAH variable from the process environment."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings(**overrides: Any) -> Settings:
    """Build Settings ignoring `.env` entirely — only the explicit overrides apply."""
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def _env_example_keys() -> set[str]:
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", stripped)
        if match:
            keys.add(match.group(1))
    return keys


def _settings_keys() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_env_example_exists() -> None:
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} is the configuration contract and must exist"


def test_every_env_example_var_is_read_by_settings() -> None:
    missing = _env_example_keys() - _settings_keys()
    assert not missing, (
        "These variables are documented in .env.example but no Settings field reads them: "
        f"{sorted(missing)}"
    )


def test_every_setting_appears_in_env_example() -> None:
    missing = _settings_keys() - _env_example_keys()
    assert not missing, (
        f"These Settings fields are not documented in .env.example: {sorted(missing)}"
    )


def test_no_secret_has_a_usable_default() -> None:
    """Master prompt §12: never ship a working default for a secret.

    Asserted against the *declared* defaults rather than a constructed instance, because
    constructing one with no secrets at all is exactly what the fail-fast validator forbids
    (see `test_jwt_secret_is_required_even_in_development`).
    """
    secret_fields = [
        "jwt_secret_key",
        "admin_initial_password",
        "anthropic_api_key",
        "openai_api_key",
        "voyage_api_key",
        "smtp_password",
        "s3_secret_access_key",
        "s3_access_key_id",
        "oidc_client_secret",
        "webhook_hmac_default_secret",
    ]

    for name in secret_fields:
        field = Settings.model_fields[name]
        default = field.default
        assert isinstance(default, SecretStr), f"{name} must be typed SecretStr"
        assert default.get_secret_value() == "", (
            f"{name} ships with a usable default — a deploy that forgets to set it would "
            f"silently run on a known secret"
        )


@pytest.mark.usefixtures("clean_env")
def test_jwt_secret_is_required_even_in_development() -> None:
    """The app cannot mint or verify a token without it, so it is never optional."""
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        make_settings()


@pytest.mark.usefixtures("clean_env")
def test_production_requires_provider_keys() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        make_settings(
            app_env="production",
            app_debug=False,
            jwt_secret_key=DUMMY_SECRET,
            admin_initial_password=DUMMY_SECRET,
            webhook_hmac_default_secret=DUMMY_SECRET,
            cors_origins="https://danah.gov",
        )


@pytest.mark.usefixtures("clean_env")
def test_production_rejects_null_cors_origin() -> None:
    """`null` exists only so the v11 HTML file can be opened from disk in dev."""
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        make_settings(
            app_env="production",
            app_debug=False,
            jwt_secret_key=DUMMY_SECRET,
            admin_initial_password=DUMMY_SECRET,
            anthropic_api_key="sk-ant-fake",
            voyage_api_key="pa-fake",
            webhook_hmac_default_secret=DUMMY_SECRET,
            cors_origins="https://danah.gov,null",
        )


@pytest.mark.usefixtures("clean_env")
def test_production_rejects_debug() -> None:
    with pytest.raises(ValueError, match="APP_DEBUG"):
        make_settings(
            app_env="production",
            app_debug=True,
            jwt_secret_key=DUMMY_SECRET,
            admin_initial_password=DUMMY_SECRET,
            anthropic_api_key="sk-ant-fake",
            voyage_api_key="pa-fake",
            webhook_hmac_default_secret=DUMMY_SECRET,
            cors_origins="https://danah.gov",
        )


@pytest.mark.usefixtures("clean_env")
def test_production_config_is_accepted_when_complete() -> None:
    """The negative tests above are only meaningful if a correct production config passes."""
    cfg = make_settings(
        app_env="production",
        app_debug=False,
        jwt_secret_key=DUMMY_SECRET,
        admin_initial_password=DUMMY_SECRET,
        anthropic_api_key="sk-ant-fake",
        voyage_api_key="pa-fake",
        webhook_hmac_default_secret=DUMMY_SECRET,
        cors_origins="https://danah.gov",
    )

    assert cfg.is_production
    assert cfg.has_llm_credentials
    assert cfg.has_embedding_credentials


@pytest.mark.usefixtures("clean_env")
def test_placeholder_secret_is_rejected() -> None:
    """The shipped `.env.example` value must not survive a copy-paste into production."""
    with pytest.raises(ValueError, match="placeholder"):
        make_settings(jwt_secret_key="CHANGE_ME_64_random_chars_min")


@pytest.mark.usefixtures("clean_env")
def test_embedding_dim_must_match_model() -> None:
    """The vector column dimension is fixed at migration time, so config must agree."""
    with pytest.raises(ValueError, match="EMBEDDING_DIM"):
        make_settings(
            jwt_secret_key=DUMMY_SECRET,
            embedding_provider="voyage",
            embedding_model="voyage-3.5",
            embedding_dim=999,
        )


@pytest.mark.usefixtures("clean_env")
def test_chunk_overlap_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError, match="CHUNK_OVERLAP_TOKENS"):
        make_settings(
            jwt_secret_key=DUMMY_SECRET,
            chunk_size_tokens=500,
            chunk_overlap_tokens=500,
        )


@pytest.mark.usefixtures("clean_env")
def test_csv_parsing() -> None:
    cfg = make_settings(
        jwt_secret_key=DUMMY_SECRET,
        cors_origins="http://a.test, http://b.test ,null",
        watch_countries="are,sau",
        allowed_upload_extensions="pdf, .docx ,TXT",
        briefing_languages="en,ar",
    )

    assert cfg.cors_origin_list == ["http://a.test", "http://b.test", "null"]
    assert cfg.watch_country_list == ["ARE", "SAU"]
    assert cfg.allowed_upload_extension_set == {"pdf", "docx", "txt"}
    assert cfg.briefing_language_list == ["en", "ar"]


@pytest.mark.usefixtures("clean_env")
def test_arabic_is_always_a_briefing_language() -> None:
    """Master prompt §12: 'Do not skip Arabic in briefings'."""
    cfg = make_settings(jwt_secret_key=DUMMY_SECRET)

    assert "ar" in cfg.briefing_language_list
    assert "en" in cfg.briefing_language_list


def test_role_clearance_matches_spec() -> None:
    """Architecture §8 clearance table."""
    assert ROLE_CLEARANCE[Role.VIEWER] is Classification.INTERNAL
    assert ROLE_CLEARANCE[Role.ANALYST] is Classification.OFFICIAL
    assert ROLE_CLEARANCE[Role.EXECUTIVE] is Classification.OFFICIAL_SENSITIVE
    assert ROLE_CLEARANCE[Role.ADMIN] is Classification.OFFICIAL_SENSITIVE
    assert set(ROLE_CLEARANCE) == set(Role)


@pytest.mark.usefixtures("clean_env")
def test_price_table_override() -> None:
    cfg = make_settings(
        jwt_secret_key=DUMMY_SECRET,
        llm_price_table='{"claude-sonnet-4-5": {"input": 9.99, "output": 19.99}}',
    )

    inp, out = cfg.price_for("claude-sonnet-4-5")
    assert float(inp) == 9.99
    assert float(out) == 19.99
    # Models absent from the override keep their built-in price.
    assert float(cfg.price_for("gpt-4o-mini")[0]) == DEFAULT_PRICE_TABLE["gpt-4o-mini"]["input"]
    # An unknown model costs 0 rather than crashing a request mid-flight.
    assert cfg.price_for("some-unknown-model") == (0, 0)
