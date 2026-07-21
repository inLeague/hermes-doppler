"""Doppler Secrets Manager plugin for Hermes Agent.

Pulls secrets from Doppler at process startup so they don't have to live
in plaintext ``~/.hermes/.env``. Supports per-profile secret isolation
for multi-profile gateway deployments.

Doppler hierarchy: Project → Environment → Config

The CLI uses ``--project`` and ``--config``; the environment is resolved
internally by Doppler and is metadata-only in this plugin.

Architecture
------------
- **Root config** — injected into ``os.environ`` (process-global).
  Every profile inherits these secrets unless overridden.
- **Profile overlays** — available only via the per-profile scope
  mechanism (``build_profile_secret_scope`` reading
  ``profiles/<name>/.env``). NOT injected into ``os.environ``.

Two modes for profile overlays:
- ``merge`` — overlay keys added on top of root (additive override)
- ``overwrite`` — overlay completely replaces root for this profile

Configuration (config.yaml)::

    secrets:
      sources: [doppler]
      doppler:
        enabled: true
        override_existing: true
        cache_ttl_seconds: 300
        timeout_seconds: 30

        root:
          project: myproject
          config: default
          token_env: DOPPLER_TOKEN
          environment: production  # metadata only

        profiles:
          staging:
            project: myproject
            config: staging
            token_env: DOPPLER_STAGING_TOKEN
            environment: staging
            mode: merge

Legacy single-config (backward-compatible)::

    secrets:
      sources: [doppler]
      doppler:
        enabled: true
        token_env: DOPPLER_TOKEN
        project: myproject
        config: staging
        override_existing: true
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.secret_sources.base import (
    SECRET_SOURCE_API_VERSION,
    ErrorKind,
    FetchResult,
    SecretSource,
    run_secret_cli,
)

logger = logging.getLogger(__name__)

# In-process cache so repeated startup calls don't re-fetch.
# Key: (configs_fingerprint,)  Value: (secrets_dict, timestamp)
_CACHE: Dict[Tuple[str, ...], Tuple[Dict[str, str], float]] = {}

_DOPPLER_INSTALL_URL = "https://docs.doppler.com/docs/cli#install"


def _fingerprint(token: str) -> str:
    """Short fingerprint for cache key from a Doppler service token."""
    return token[:8] + "…" + token[-4:] if len(token) > 16 else token[:4]


def _check_doppler_available() -> Optional[str]:
    """Check if the doppler CLI is available on PATH.

    Returns None if found, or an error message string if not found.
    """
    if shutil.which("doppler") is None:
        return (
            "doppler CLI not found on PATH. Install it from "
            f"{_DOPPLER_INSTALL_URL}"
        )
    return None


def _fetch_one_config(
    *,
    project: str,
    config: str,
    token: str,
    token_env: str,
    timeout: float,
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Fetch secrets from a single Doppler config.

    Returns (secrets_dict, error_string).  On success error is None;
    on failure secrets_dict is None.
    """
    argv = [
        "doppler", "secrets", "download",
        "--project", project,
        "--config", config,
        "--format", "json",
        "--no-file",
    ]

    try:
        proc = run_secret_cli(
            argv,
            allow_env=[token_env],
            extra_env={"DOPPLER_TOKEN": token},
            timeout=timeout,
        )
    except RuntimeError as exc:
        return None, f"doppler CLI failed ({project}/{config}): {exc}"

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if "unauthorized" in stderr.lower() or "invalid token" in stderr.lower():
            return None, f"Doppler auth failed ({project}/{config}): {stderr}"
        elif proc.returncode == 78:
            return None, f"Doppler config not found ({project}/{config}): {stderr}"
        else:
            return None, f"doppler exited {proc.returncode} ({project}/{config}): {stderr}"

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None, f"doppler returned empty output ({project}/{config})"

    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, f"failed to parse doppler JSON ({project}/{config}): {exc}"

    if not isinstance(raw, dict):
        return None, f"expected JSON object, got {type(raw).__name__} ({project}/{config})"

    flat: Dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(v, str):
            flat[k] = v
        elif isinstance(v, (int, float, bool)):
            flat[k] = str(v)

    if not flat:
        return None, f"doppler returned no secrets ({project}/{config})"

    return flat, None


def _parse_root(cfg: dict) -> Optional[Tuple[str, str, str]]:
    """Parse root config entry. Returns (project, config, token_env) or None."""
    root = cfg.get("root")
    if isinstance(root, dict):
        project = root.get("project", "")
        config = root.get("config", "")
        token_env = root.get("token_env", "DOPPLER_TOKEN")
        if project and config:
            return (project, config, token_env)

    # Legacy single-config fallback
    project = cfg.get("project", "")
    config = cfg.get("config", "")
    token_env = cfg.get("token_env", "DOPPLER_TOKEN")
    if project and config:
        return (project, config, token_env)

    return None


def _parse_profiles(cfg: dict) -> Dict[str, Dict[str, Any]]:
    """Parse profile overlay entries. Returns {name: {project, config, token_env, mode, environment}}."""
    profiles: Dict[str, Dict[str, Any]] = {}
    raw = cfg.get("profiles")
    if not isinstance(raw, dict):
        return profiles

    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        project = entry.get("project", "")
        config = entry.get("config", "")
        token_env = entry.get("token_env", "DOPPLER_TOKEN")
        mode = entry.get("mode", "merge")
        environment = entry.get("environment", "")
        if project and config:
            profiles[name] = {
                "project": project,
                "config": config,
                "token_env": token_env,
                "mode": mode,
                "environment": environment,
            }

    return profiles


class DopplerSource(SecretSource):
    """Resolve secrets from Doppler Secrets Manager."""

    name = "doppler"
    label = "Doppler"
    shape = "bulk"
    api_version = SECRET_SOURCE_API_VERSION

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        cfg = cfg if isinstance(cfg, dict) else {}
        result = FetchResult()

        # ── Pre-flight: check doppler CLI is available ──────────────
        doppler_err = _check_doppler_available()
        if doppler_err:
            result.error = doppler_err
            result.error_kind = ErrorKind.NOT_CONFIGURED
            return result

        timeout = float(cfg.get("timeout_seconds", 30))
        cache_ttl = float(cfg.get("cache_ttl_seconds", 300))

        # ── Parse configuration ─────────────────────────────────────
        root_entry = _parse_root(cfg)
        profiles = _parse_profiles(cfg)

        if not root_entry:
            result.error = (
                "Doppler: 'root' config (or legacy project/config) is required"
            )
            result.error_kind = ErrorKind.NOT_CONFIGURED
            return result

        root_project, root_config, root_token_env = root_entry

        # ── Resolve root token ──────────────────────────────────────
        root_token = os.environ.get(root_token_env, "")
        if not root_token:
            result.error = (
                f"env var {root_token_env} is not set — add your Doppler service "
                f"token to ~/.hermes/.env as {root_token_env}=<token>"
            )
            result.error_kind = ErrorKind.NOT_CONFIGURED
            return result

        # ── Resolve profile tokens ──────────────────────────────────
        resolved_profiles: Dict[str, Tuple[str, str, str, str, str]] = {}
        for name, p in profiles.items():
            token = os.environ.get(p["token_env"], "")
            if not token:
                result.error = (
                    f"env var {p['token_env']} is not set for profile '{name}' — "
                    f"add your Doppler service token as {p['token_env']}=<token>"
                )
                result.error_kind = ErrorKind.NOT_CONFIGURED
                return result
            resolved_profiles[name] = (
                p["project"],
                p["config"],
                p["token_env"],
                token,
                p["mode"],
            )

        # ── Cache check ─────────────────────────────────────────────
        cache_key_parts: List[str] = [f"root:{_fingerprint(root_token)}:{root_project}:{root_config}"]
        for name in sorted(resolved_profiles):
            p = resolved_profiles[name]
            cache_key_parts.append(f"profile:{name}:{_fingerprint(p[3])}:{p[0]}:{p[1]}")
        cache_key = tuple(cache_key_parts)

        if cache_ttl > 0 and cache_key in _CACHE:
            cached_secrets, cached_at = _CACHE[cache_key]
            if time.time() - cached_at < cache_ttl:
                logger.debug("Doppler: using cached secrets (%d vars)", len(cached_secrets))
                result.secrets = cached_secrets
                return result

        # ── Fetch root config ───────────────────────────────────────
        root_secrets, root_err = _fetch_one_config(
            project=root_project,
            config=root_config,
            token=root_token,
            token_env=root_token_env,
            timeout=timeout,
        )

        if root_err:
            result.error = root_err
            result.error_kind = ErrorKind.NETWORK
            return result

        if not root_secrets:
            result.error = f"doppler returned no secrets from root config ({root_project}/{root_config})"
            result.error_kind = ErrorKind.EMPTY_VALUE
            return result

        logger.info(
            "Doppler: fetched %d secrets from root %s/%s",
            len(root_secrets), root_project, root_config,
        )

        # ── Fetch profile configs ───────────────────────────────────
        profile_secrets: Dict[str, Dict[str, str]] = {}
        profile_errors: List[str] = []

        for name, (project, config, token_env, token, mode) in resolved_profiles.items():
            secrets, err = _fetch_one_config(
                project=project,
                config=config,
                token=token,
                token_env=token_env,
                timeout=timeout,
            )
            if err:
                profile_errors.append(f"profile '{name}': {err}")
                continue

            if secrets:
                profile_secrets[name] = secrets
                logger.info(
                    "Doppler: fetched %d secrets from profile %s/%s (mode=%s — NOT injected into os.environ)",
                    len(secrets), project, config, mode,
                )

        if profile_errors:
            logger.warning("Doppler: profile fetch errors — %s", "; ".join(profile_errors))

        # ── Inject root into os.environ ─────────────────────────────
        # The root config goes into result.secrets which the core
        # injects into os.environ. Profile overlays are NOT injected —
        # they're available only via the profile scope mechanism.
        if cache_ttl > 0:
            _CACHE[cache_key] = (root_secrets, time.time())

        total_profile = sum(len(s) for s in profile_secrets.values())
        logger.info(
            "Doppler: %d secrets injected into os.environ (root), "
            "%d overlay secrets held for %d profile(s)",
            len(root_secrets), total_profile, len(profile_secrets),
        )

        result.secrets = root_secrets
        return result

    def is_enabled(self, cfg: dict) -> bool:
        return bool(isinstance(cfg, dict) and cfg.get("enabled"))

    def override_existing(self, cfg: dict) -> bool:
        return bool(isinstance(cfg, dict) and cfg.get("override_existing", True))

    def protected_env_vars(self, cfg: dict) -> frozenset:
        """Protect all bootstrap tokens from being overwritten by Doppler."""
        cfg = cfg if isinstance(cfg, dict) else {}
        protected = set()

        # Root token
        root = cfg.get("root")
        if isinstance(root, dict):
            protected.add(root.get("token_env", "DOPPLER_TOKEN"))
        else:
            protected.add(cfg.get("token_env", "DOPPLER_TOKEN"))

        # Profile tokens
        profiles = cfg.get("profiles")
        if isinstance(profiles, dict):
            for entry in profiles.values():
                if isinstance(entry, dict):
                    protected.add(entry.get("token_env", "DOPPLER_TOKEN"))

        return frozenset(protected)

    def config_schema(self) -> dict:
        return {
            "enabled": {"description": "Enable the Doppler secret source", "default": False},
            "override_existing": {"description": "Overwrite existing env vars from .env", "default": True},
            "cache_ttl_seconds": {"description": "In-process cache TTL (0 to disable)", "default": 300},
            "timeout_seconds": {"description": "Timeout for each doppler CLI call", "default": 30},
            "root": {
                "description": (
                    "Root config — injected into os.environ (process-global). "
                    "Fields: project, config, token_env, environment (metadata)."
                ),
                "default": {},
            },
            "profiles": {
                "description": (
                    "Per-profile overlays — available via profile scope only, NOT injected into os.environ. "
                    "Each entry: {project, config, token_env, mode (merge|overwrite), environment}."
                ),
                "default": {},
            },
            # Legacy single-config (backward-compatible)
            "token_env": {"description": "Env var holding the Doppler service token (legacy)", "default": "DOPPLER_TOKEN"},
            "project": {"description": "Doppler project name (legacy)", "default": ""},
            "config": {"description": "Doppler config name (legacy)", "default": ""},
        }


def register(ctx):
    """Register the Doppler secret source with Hermes."""
    ctx.register_secret_source(DopplerSource())
