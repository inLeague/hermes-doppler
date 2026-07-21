# hermes-doppler

Doppler Secrets Manager plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — per-profile secret isolation for multi-profile gateway deployments.

## Why

Hermes Agent's multi-profile gateway runs multiple agents (e.g. Karlin + Whitworth) in a single process. Each profile needs its own secrets (Slack tokens, API keys, etc.), but the process environment is shared. Without isolation, profile A's secrets leak to profile B via `os.environ`.

This plugin solves the problem by separating secrets into:

- **Root config** — injected into `os.environ` (process-global, inherited by all profiles)
- **Profile overlays** — available only via the per-profile scope mechanism, NOT injected into `os.environ`

## Doppler Hierarchy

Doppler organizes secrets as: **Project → Environment → Config**

The CLI uses `--project` and `--config`; the environment is resolved internally by Doppler and is metadata-only in this plugin.

## Install

```bash
# As a Hermes plugin (recommended)
hermes plugins install https://github.com/inLeague/hermes-doppler

# Or manually — clone into ~/.hermes/plugins/
git clone https://github.com/inLeague/hermes-doppler.git ~/.hermes/plugins/doppler_secrets
```

## Configuration

### Root + Profiles (recommended for multi-profile)

```yaml
secrets:
  sources: [doppler]
  doppler:
    enabled: true
    override_existing: true
    cache_ttl_seconds: 300
    timeout_seconds: 30

    # Root — injected into os.environ (process-global)
    root:
      project: karlin
      config: default                    # Doppler config name
      token_env: DOPPLER_KARLIN_DEFAULT_TOKEN
      environment: Default-Karlin        # metadata only

    # Profile overlays — NOT injected into os.environ
    # Available only via the profile scope mechanism
    profiles:
      whitworth:
        project: karlin
        config: whitworth
        token_env: DOPPLER_KARLIN_WHITWORTH_TOKEN
        environment: whitworth
        mode: merge                      # merge | overwrite
```

### Profile Overlay Modes

| Mode | Behavior |
|------|----------|
| `merge` | Overlay keys added on top of root (additive override) |
| `overwrite` | Overlay completely replaces root for this profile |

### Legacy Single-Config (backward-compatible)

```yaml
secrets:
  sources: [doppler]
  doppler:
    enabled: true
    token_env: DOPPLER_TOKEN
    project: inleague
    config: staging
    override_existing: true
```

## How It Works

1. At gateway startup, the plugin fetches the root config from Doppler and injects its secrets into `os.environ`
2. Profile overlay configs are fetched but NOT injected into `os.environ`
3. When a profile's turn runs, `build_profile_secret_scope()` reads the profile's `.env` file and installs it as a context-local scope
4. `get_secret()` reads from the scope (for profile turns) or `os.environ` (for the default profile)
5. The profile scope provides the overlay secrets, so each profile sees its own isolated values

## Token Setup

Each Doppler config needs a service token. Store tokens in systemd EnvironmentFiles for defense in depth:

```bash
# /etc/hermes/karlin-doppler.env (root:hermes, 0600)
DOPPLER_KARLIN_DEFAULT_TOKEN=dp.st.xxxx
DOPPLER_KARLIN_WHITWORTH_TOKEN=dp.st.yyyy
```

Wire into systemd:

```ini
[Service]
EnvironmentFile=/etc/hermes/karlin-doppler.env
```

## License

MIT — see [LICENSE](LICENSE).
