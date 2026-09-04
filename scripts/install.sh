#!/usr/bin/env bash
# One-shot install for buzz-fleet: builds both binaries (the Rust signer and
# the standalone Python/Textual TUI) and installs them to /usr/local/bin.
# Safe to re-run — it rebuilds and reinstalls both binaries every time.
#
# Prerequisites this script installs if missing: Rust (via rustup), uv (via
# the official installer). Everything else it needs is fetched by cargo/uv.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> buzz-fleet install: $REPO_ROOT"

# --- Rust toolchain ---------------------------------------------------------
if ! command -v cargo >/dev/null 2>&1; then
  echo "==> cargo not found — installing Rust via rustup"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  # shellcheck disable=SC1091
  source "$HOME/.cargo/env"
else
  echo "==> cargo found: $(command -v cargo)"
fi

# --- uv (Python package/tool manager, build-time only) ----------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "==> uv not found — installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
else
  echo "==> uv found: $(command -v uv)"
fi

# --- Build and install buzz-fleet-signer (Rust) -----------------------------
echo "==> Building buzz-fleet-signer (release)"
(cd signer && cargo build --release)
echo "==> Installing buzz-fleet-signer to /usr/local/bin (requires sudo)"
sudo install -m 0755 signer/target/release/buzz-fleet-signer /usr/local/bin/buzz-fleet-signer

# --- Build and install buzz-fleet (standalone Python/Textual binary) -------
echo "==> Syncing Python dependencies"
uv sync --group dev

echo "==> Building standalone buzz-fleet binary (PyInstaller)"
uv run pyinstaller --onefile --name buzz-fleet --paths src \
  --collect-all textual --collect-all rich --collect-all pydantic \
  --collect-all typer --collect-all click \
  scripts/pyinstaller_entry.py

echo "==> Installing buzz-fleet to /usr/local/bin (requires sudo)"
sudo install -m 0755 dist/buzz-fleet /usr/local/bin/buzz-fleet

echo ""
echo "==> Done. Installed:"
echo "      $(command -v buzz-fleet-signer)"
echo "      $(command -v buzz-fleet)"
echo ""
echo "==> Next: run 'buzz-fleet tui' and connect to your community."
