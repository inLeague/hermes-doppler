# hermes-doppler

[Doppler](https://doppler.com) Secrets Manager plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — per-profile secret isolation for multi-profile gateway deployments.

## Why

Hermes supports Bitwarden and 1Password natively. With this plugin, you can use Doppler secrets. You still need to store your doppler token in .env (though we can do better once Hermes' systemd template supports EnvironmentFile) but it's better than storing ALL your keys in .env!

This plugin supports system-wide and per-profile secrets:

- **Root config** — injected into `os.environ` (process-global, inherited by all profiles)
- **Profile overlays** — available only via the per-profile scope mechanism, NOT injected into `os.environ`

## Doppler Hierarchy

Doppler organizes secrets as: **Project → Environment → Config**

The CLI uses `--project` and `--config`; the environment is resolved internally by Doppler and is metadata-only in this plugin.

## Install

```bash
# As a Hermes plugin (recommended)
hermes plugins install https://github.com/inLeague/hermes-doppler

# Or manually — clone into ~/.hermes/plugins/doppler_secrets
git clone https://github.com/inLeague/hermes-doppler.git ~/.hermes/plugins/doppler_secrets
```

**Important:** When cloning manually, the target directory MUST be named `doppler_secrets` for Hermes to discover the plugin correctly.

After installing, restart the gateway so the plugin loads once. That load installs the startup bootstrap (see [Post-Install and Post-Update Setup](#post-install-and-post-update-setup)) — no manual step is required after later updates.

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
      project: myproject
      config: default                    # Doppler config name
      token_env: DOPPLER_TOKEN
      environment: production            # metadata only

    # Profile overlays — NOT injected into os.environ
    # Available only via the profile scope mechanism
    profiles:
      staging:
        project: myproject
        config: staging
        token_env: DOPPLER_PROFILENAME_TOKEN
        environment: staging
        mode: merge                      # merge | overwrite
```

### Profile Overlay Modes

| Mode | Behavior |
| --- | --- |
| `merge` | Overlay keys added on top of root (additive override) |
| `overwrite` | Overlay completely replaces root for this profile |

### Single-Config Mode
If you don't run multiple agent profiles, you can supply a token for a single config. You can also use the multi-profile config but only specify root if you think you might add profiles later.

```yaml
secrets:
  sources: [doppler]
  doppler:
    enabled: true
    token_env: DOPPLER_TOKEN
    project: myproject
    config: staging
    override_existing: true
```

## How It Works

1. At gateway startup, the plugin fetches the root config from Doppler and injects its secrets into `os.environ` (with the startup bootstrap installed, the source is registered at interpreter startup, so this happens on the gateway's first env load)
2. Profile overlay configs are fetched but NOT injected into `os.environ`
3. When a profile's turn runs, `build_profile_secret_scope()` reads the profile's `.env` file and installs it as a context-local scope
4. `get_secret()` reads from the scope (for profile turns) or `os.environ` (for the default profile)
5. The profile scope provides the overlay secrets, so each profile sees its own isolated values

## Token Setup

Each Doppler config needs a service token. Add your tokens to `~/.hermes/.env`:

```bash
# ~/.hermes/.env
DOPPLER_TOKEN=dp.st.xxxx
DOPPLER_PROFILENAME_TOKEN=dp.st.yyyy
```

Hermes loads `.env` before running secret sources, so the tokens are available when the plugin fetches from Doppler.

### Security Note

For stronger isolation, Doppler service tokens could be stored in root-owned files (e.g. `/etc/hermes/doppler-tokens.env` with `chmod 0600`) and loaded via systemd `EnvironmentFile=`. However, Hermes regenerates the gateway's systemd unit on restart (`generate_systemd_unit()`), which overwrites any custom `EnvironmentFile=` directives. Until upstream supports preserving custom environment files, tokens must live in `~/.hermes/.env`.

## Post-Install and Post-Update Setup

After installing the plugin, run the setup script so the Doppler source is registered **before** `load_hermes_dotenv()` runs:

```bash
python3 ~/.hermes/plugins/doppler-secrets/setup_sitecustomize.py
```

**PM runtime (current Hermes):** the script writes a `sitecustomize.py` into each managed store Python's `site-packages/` (`~/.hermes/tools/python-*/`). That bootstrap registers the Doppler source at interpreter startup — before the gateway's first env load and before plugin discovery — which eliminates the startup warning and also makes the source available to standalone child processes (e.g. cron workers). To import the plugin and registry, the bootstrap temporarily adds the Hermes checkout and the install's PM-selected dependency environment to `sys.path`, then restores `sys.path` exactly as it found it.

**Legacy (pre-PM) hosts:** the script falls back to installing into whichever `sitecustomize.py` the running Python would import (the original behaviour).

**The plugin now self-heals the bootstrap.** Every time the plugin loads, `ensure_startup_bootstrap()` checks the running interpreter's `site-packages` and (re)writes the bootstrap if it is missing or stale — so a runtime recreated by `hermes update` is repaired on the first process that loads plugins, and every process after that is warning-free. The script above is therefore only needed to make the *next* boot clean (before any plugin load has happened in that runtime) or on a host that has never booted Hermes:

```bash
hermes update
# optional — the plugin repairs the bootstrap on its next load anyway
python3 ~/.hermes/plugins/doppler-secrets/setup_sitecustomize.py
```

Self-heal is limited to runtimes Hermes owns (`site-packages` under `~/.hermes`); it never touches a system Python or an unrelated venv. Set `HERMES_DOPPLER_NO_BOOTSTRAP=1` to disable it.

Both paths share `bootstrap.py`, so the block text and write logic cannot drift; the setup script is idempotent and rewrites old bootstrap blocks cleanly.

## Known Limitations

### Startup warning on the first boot of a fresh runtime

`sitecustomize.py` must exist before the interpreter starts, so the very first process on a runtime that has no bootstrap (a fresh install, or the first boot after `hermes update` recreated the runtime) still logs the cosmetic `secrets.sources names unknown source(s): doppler` warning while registering the source moments later. The plugin installs the bootstrap during that boot, so the warning stops there — run the setup script if you want even that first boot clean. Nothing is lost either way: the boot re-applies the vault right after discovery.

## License

MIT — see [LICENSE](LICENSE).
