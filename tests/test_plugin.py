"""Tests for hermes_doppler plugin."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# We need to mock the agent.secret_sources.base module since it's only
# available inside the Hermes Agent runtime.
import sys

# Create mock module hierarchy
agent_mock = MagicMock()
secret_sources_mock = MagicMock()
base_mock = MagicMock()

# Set up the mock base module with the constants/classes the plugin needs
base_mock.SECRET_SOURCE_API_VERSION = 2
base_mock.ErrorKind = MagicMock()
base_mock.ErrorKind.NOT_CONFIGURED = "NOT_CONFIGURED"
base_mock.ErrorKind.NETWORK = "NETWORK"
base_mock.ErrorKind.EMPTY_VALUE = "EMPTY_VALUE"

# Make FetchResult work as a proper dataclass-like
class MockFetchResult:
    def __init__(self):
        self.secrets = {}
        self.error = None
        self.error_kind = None
    @property
    def ok(self):
        return self.error is None

base_mock.FetchResult = MockFetchResult

class MockSecretSource:
    pass

base_mock.SecretSource = MockSecretSource

def mock_run_secret_cli(argv, allow_env=None, extra_env=None, timeout=30):
    """Mock run_secret_cli that returns test data."""
    # Extract project and config from argv
    project = None
    config = None
    for i, arg in enumerate(argv):
        if arg == "--project" and i + 1 < len(argv):
            project = argv[i + 1]
        if arg == "--config" and i + 1 < len(argv):
            config = argv[i + 1]

    # Return test secrets based on project/config
    test_data = {
        ("karlin", "default"): {
            "SLACK_BOT_TOKEN": "xoxb-karlin-default",
            "SLACK_APP_TOKEN": "xapp-karlin-default",
            "API_KEY": "key-karlin-default",
        },
        ("karlin", "whitworth"): {
            "SLACK_BOT_TOKEN": "xoxb-whitworth",
            "SLACK_APP_TOKEN": "xapp-whitworth",
            "WHITWORTH_ONLY": "secret-whitworth",
        },
    }

    secrets = test_data.get((project, config), {})
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = json.dumps(secrets)
    proc.stderr = ""
    return proc

base_mock.run_secret_cli = mock_run_secret_cli

# Install mocks
sys.modules["agent"] = agent_mock
sys.modules["agent.secret_sources"] = secret_sources_mock
sys.modules["agent.secret_sources.base"] = base_mock

# Now import the plugin
from hermes_doppler import (
    DopplerSource,
    _fetch_one_config,
    _parse_profiles,
    _parse_root,
)


class TestParseRoot:
    def test_new_style_root(self):
        cfg = {
            "root": {
                "project": "karlin",
                "config": "default",
                "token_env": "DOPPLER_TOKEN",
                "environment": "production",
            }
        }
        result = _parse_root(cfg)
        assert result == ("karlin", "default", "DOPPLER_TOKEN")

    def test_legacy_single_config(self):
        cfg = {
            "project": "inleague",
            "config": "staging",
            "token_env": "DOPPLER_TOKEN",
        }
        result = _parse_root(cfg)
        assert result == ("inleague", "staging", "DOPPLER_TOKEN")

    def test_missing_project(self):
        cfg = {"root": {"config": "default"}}
        result = _parse_root(cfg)
        assert result is None

    def test_missing_config(self):
        cfg = {"root": {"project": "karlin"}}
        result = _parse_root(cfg)
        assert result is None

    def test_empty_config(self):
        cfg = {}
        result = _parse_root(cfg)
        assert result is None


class TestParseProfiles:
    def test_single_profile(self):
        cfg = {
            "profiles": {
                "whitworth": {
                    "project": "karlin",
                    "config": "whitworth",
                    "token_env": "DOPPLER_TOKEN_W",
                    "mode": "merge",
                    "environment": "staging",
                }
            }
        }
        result = _parse_profiles(cfg)
        assert "whitworth" in result
        assert result["whitworth"]["project"] == "karlin"
        assert result["whitworth"]["config"] == "whitworth"
        assert result["whitworth"]["mode"] == "merge"

    def test_multiple_profiles(self):
        cfg = {
            "profiles": {
                "dev": {"project": "p", "config": "dev"},
                "staging": {"project": "p", "config": "staging"},
            }
        }
        result = _parse_profiles(cfg)
        assert len(result) == 2
        assert "dev" in result
        assert "staging" in result

    def test_default_mode_is_merge(self):
        cfg = {"profiles": {"x": {"project": "p", "config": "c"}}}
        result = _parse_profiles(cfg)
        assert result["x"]["mode"] == "merge"

    def test_empty_profiles(self):
        cfg = {}
        result = _parse_profiles(cfg)
        assert result == {}

    def test_invalid_entry_skipped(self):
        cfg = {"profiles": {"bad": "not-a-dict"}}
        result = _parse_profiles(cfg)
        assert result == {}

    def test_missing_project_skipped(self):
        cfg = {"profiles": {"bad": {"config": "c"}}}
        result = _parse_profiles(cfg)
        assert result == {}


class TestFetchOneConfig:
    def test_successful_fetch(self):
        secrets, err = _fetch_one_config(
            project="karlin",
            config="default",
            token="dp.st.test",
            token_env="DOPPLER_TOKEN",
            timeout=5,
        )
        assert err is None
        assert secrets is not None
        assert secrets["SLACK_BOT_TOKEN"] == "xoxb-karlin-default"

    def test_different_configs_return_different_secrets(self):
        s1, _ = _fetch_one_config(
            project="karlin", config="default",
            token="dp.st.test", token_env="T", timeout=5,
        )
        s2, _ = _fetch_one_config(
            project="karlin", config="whitworth",
            token="dp.st.test", token_env="T", timeout=5,
        )
        assert s1["SLACK_BOT_TOKEN"] == "xoxb-karlin-default"
        assert s2["SLACK_BOT_TOKEN"] == "xoxb-whitworth"
        assert s1 != s2


class TestDopplerSource:
    def setup_method(self):
        self.source = DopplerSource()

    def test_is_enabled_true(self):
        assert self.source.is_enabled({"enabled": True}) is True

    def test_is_enabled_false(self):
        assert self.source.is_enabled({"enabled": False}) is False

    def test_is_enabled_missing(self):
        assert self.source.is_enabled({}) is False

    def test_override_existing_default(self):
        assert self.source.override_existing({}) is True

    def test_override_existing_false(self):
        assert self.source.override_existing({"override_existing": False}) is False

    def test_protected_env_vars_root_only(self):
        cfg = {"root": {"token_env": "TOKEN_A"}}
        protected = self.source.protected_env_vars(cfg)
        assert "TOKEN_A" in protected

    def test_protected_env_vars_with_profiles(self):
        cfg = {
            "root": {"token_env": "TOKEN_A"},
            "profiles": {
                "w": {"token_env": "TOKEN_B"},
            },
        }
        protected = self.source.protected_env_vars(cfg)
        assert "TOKEN_A" in protected
        assert "TOKEN_B" in protected

    def test_protected_env_vars_legacy(self):
        cfg = {"token_env": "LEGACY_TOKEN"}
        protected = self.source.protected_env_vars(cfg)
        assert "LEGACY_TOKEN" in protected

    def test_fetch_root_only(self, monkeypatch):
        monkeypatch.setenv("DOPPLER_TOKEN", "dp.st.test")
        result = self.source.fetch(
            {"root": {"project": "karlin", "config": "default", "token_env": "DOPPLER_TOKEN"}},
            Path("/tmp"),
        )
        assert result.ok
        assert result.secrets["SLACK_BOT_TOKEN"] == "xoxb-karlin-default"
        assert result.secrets["API_KEY"] == "key-karlin-default"

    def test_fetch_root_not_injected_with_profile_overrides(self, monkeypatch):
        """Profile overlay secrets must NOT appear in result.secrets (os.environ)."""
        monkeypatch.setenv("DOPPLER_TOKEN", "dp.st.test")
        monkeypatch.setenv("DOPPLER_TOKEN_W", "dp.st.test")
        result = self.source.fetch(
            {
                "root": {"project": "karlin", "config": "default", "token_env": "DOPPLER_TOKEN"},
                "profiles": {
                    "whitworth": {
                        "project": "karlin",
                        "config": "whitworth",
                        "token_env": "DOPPLER_TOKEN_W",
                        "mode": "merge",
                    }
                },
            },
            Path("/tmp"),
        )
        assert result.ok
        # Root secrets present
        assert result.secrets["SLACK_BOT_TOKEN"] == "xoxb-karlin-default"
        # Profile-only secret NOT present
        assert "WHITWORTH_ONLY" not in result.secrets

    def test_fetch_missing_root_token(self, monkeypatch):
        monkeypatch.delenv("DOPPLER_TOKEN", raising=False)
        result = self.source.fetch(
            {"root": {"project": "karlin", "config": "default", "token_env": "DOPPLER_TOKEN"}},
            Path("/tmp"),
        )
        assert not result.ok
        assert "DOPPLER_TOKEN" in result.error

    def test_fetch_missing_profile_token(self, monkeypatch):
        monkeypatch.setenv("DOPPLER_TOKEN", "dp.st.test")
        monkeypatch.delenv("DOPPLER_TOKEN_W", raising=False)
        result = self.source.fetch(
            {
                "root": {"project": "karlin", "config": "default", "token_env": "DOPPLER_TOKEN"},
                "profiles": {
                    "whitworth": {
                        "project": "karlin",
                        "config": "whitworth",
                        "token_env": "DOPPLER_TOKEN_W",
                    }
                },
            },
            Path("/tmp"),
        )
        assert not result.ok
        assert "DOPPLER_TOKEN_W" in result.error

    def test_fetch_no_root_config(self):
        result = self.source.fetch({}, Path("/tmp"))
        assert not result.ok
        assert "root" in result.error.lower()

    def test_config_schema_has_root_and_profiles(self):
        schema = self.source.config_schema()
        assert "root" in schema
        assert "profiles" in schema
        assert "project" in schema  # legacy
        assert "config" in schema  # legacy
