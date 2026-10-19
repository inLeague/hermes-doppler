#!/usr/bin/env python3
"""Install the Doppler startup bootstrap into the Python runtimes that import it.

PM runtime: Hermes runs on a managed *store Python* (``~/.hermes/tools/python-*/``)
plus per-install dependency environments.  The bootstrap lives in each store
Python's ``site-packages/sitecustomize.py`` so the Doppler secret source is
registered before ``load_hermes_dotenv()`` runs — in gateways, CLI runs, and any
child process spawned on the same interpreter.

The plugin now re-installs the bootstrap by itself whenever it loads (see
``ensure_startup_bootstrap()`` in ``__init__.py``), so this script is only
needed to make the *next* boot warning-free — e.g. right after an update
recreated the runtime, when the plugin has not loaded in that runtime yet — or
on a host that has never booted Hermes.

    python3 ~/.hermes/plugins/doppler-secrets/setup_sitecustomize.py

Idempotent and safe to re-run.  The block text and write logic live in
``bootstrap.py`` so this script and the self-heal path cannot drift.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent


def load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "hermes_doppler_bootstrap", str(PLUGIN_DIR / "bootstrap.py"))
    if spec is None or spec.loader is None:
        raise SystemExit(f"ERROR: bootstrap.py not found beside {__file__}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_repo_root() -> Path:
    """Best-effort: the Hermes checkout the launchers actually bind."""
    default = Path.home() / ".hermes" / "hermes-agent"
    launcher = default / ".hermes" / "bin" / "hermes"
    try:
        match = re.search(r"(/[\w./+-]*/hermes-agent)", launcher.read_text())
        if match:
            return Path(match.group(1))
    except OSError:
        pass
    return default


def store_pythons() -> list[Path]:
    return sorted((Path.home() / ".hermes" / "tools").glob("python-*/bin/python3"))


def site_packages_of(python: Path) -> Path | None:
    try:
        out = subprocess.run(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        candidate = Path(out.stdout.strip())
        return candidate if candidate.is_dir() else None
    except Exception:
        return None


def active_sitecustomize() -> Path | None:
    """Legacy path: the sitecustomize.py the RUNNING Python would import."""
    spec = importlib.util.find_spec("sitecustomize")
    if spec and spec.origin:
        return Path(spec.origin)
    return None


def main() -> int:
    bootstrap = load_bootstrap()
    repo = None
    argv = sys.argv[1:]
    if "--repo-root" in argv:
        repo = Path(argv[argv.index("--repo-root") + 1]).expanduser()
    elif argv and not argv[0].startswith("-"):
        repo = Path(argv[0]).expanduser()
    if repo is None:
        repo = find_repo_root()

    if not repo.is_dir():
        print(f"WARNING: repo root not found at {repo}; installing the bootstrap anyway "
              "(it no-ops until the checkout exists).", file=sys.stderr)

    targets = [(py, sp) for py in store_pythons() if (sp := site_packages_of(py))]

    if targets:
        statuses = []
        for py, sp in targets:
            status = bootstrap.ensure(repo_root=repo, target=sp)
            statuses.append(status)
            print(f"[{status}] {sp / 'sitecustomize.py'}  (for {py})")
        if "unwritable" in statuses:
            print("ERROR: at least one site-packages directory could not be written.",
                  file=sys.stderr)
            return 1
    else:
        active = active_sitecustomize()
        if active is None:
            print("ERROR: no PM store Python found, and the running Python imports no "
                  "sitecustomize — cannot determine an install target.", file=sys.stderr)
            return 1
        status = bootstrap.write_block(active, bootstrap.block_for(repo, legacy=True))
        print(f"[{status}] {active}  (legacy mode)")
        if status == "unwritable":
            return 1

    print("Restart the gateway for the change to take effect.")
    print("The plugin re-installs this by itself on load, so later runtime "
          "recreations (hermes update / PM toolchain updates) need no manual step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
