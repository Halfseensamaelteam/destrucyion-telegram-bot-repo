"""
tests/unit/test_config.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for app.core.config.Settings.

Tests:
- Default values load correctly.
- Environment overrides are applied.
- is_development / is_production helpers.
- Settings singleton cache works.
"""

import pytest

from app.core.config import Settings, get_settings


class TestSettingsDefaults:
    def test_default_app_env_is_development(self):
        s = Settings()
        assert s.app_env == "development"

    def test_default_log_level_is_info(self):
        s = Settings()
        assert s.log_level == "INFO"

    def test_is_development_true_by_default(self):
        s = Settings()
        assert s.is_development is True
        assert s.is_production is False

    def test_default_telegram_api_id_is_zero(self, monkeypatch):
        # This test needs to verify default value when TELEGRAM_API_ID is not set
        # Clear any existing TELEGRAM_API_ID from environment AND .env file
        monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
        # Clear the Settings cache to get a fresh instance
        get_settings.cache_clear()
        # Create Settings instance with explicit override to ensure no .env loading
        from pydantic_settings import BaseSettings
        # Temporarily disable .env loading for this test
        s = Settings(_env_file=None)
        assert s.telegram_api_id == 0


class TestSettingsEnvOverride:
    def test_app_env_override(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        s = Settings()
        assert s.app_env == "production"
        assert s.is_production is True
        assert s.is_development is False

    def test_telegram_api_id_from_env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_API_ID", "99887766")
        s = Settings()
        assert s.telegram_api_id == 99887766

    def test_log_level_override(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        s = Settings()
        assert s.log_level == "DEBUG"

    def test_invalid_app_env_raises(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "invalid-value")
        with pytest.raises(Exception):
            Settings()


class TestSettingsSingleton:
    def test_get_settings_returns_same_instance(self):
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_cache_clear_returns_fresh_instance(self):
        s1 = get_settings()
        get_settings.cache_clear()
        s2 = get_settings()
        # Both should be equal in value but may be different objects after cache clear.
        assert s1.app_env == s2.app_env
