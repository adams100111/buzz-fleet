#!/usr/bin/env bash
# scripts/get.sh — buzz-fleet public bootstrap.
# Usage: curl -fsSL https://raw.githubusercontent.com/adams100111/buzz-fleet/main/scripts/get.sh | bash
# Downloads the arch-matched buzz-fleet + buzz-fleet-signer binaries from the
# latest GitHub Release, verifies SHA256, and installs both onto PATH (via a
# symlink in the user bin dir) — no Rust, no Python, no uv, no clone. Zero
# logic beyond fetch/verify/link.
set -Eeuo pipefail

GS_REPO="adams100111/buzz-fleet"
GS_BASE="https://github.com/${GS_REPO}/releases/latest/download"
GS_PREFIX="${HOME}/.local/share/buzz-fleet"
GS_PERSONAS_DIR="${HOME}/.config/buzz-fleet/personas"

gs_err() { echo "get.sh: $*" >&2; }

gs_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo x86_64 ;;
    aarch64|arm64) echo aarch64 ;;
    *) gs_err "unsupported architecture: $(uname -m) (x86_64/aarch64 only)"; return 1 ;;
  esac
}

gs_fetch() {
  local url="$1" out="$2"
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$url" -o "$out"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$out" "$url"
  else gs_err "need curl or wget"; return 1; fi
}

gs_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$@"
  else gs_err "need sha256sum or shasum"; return 1; fi
}

# gs_verify DIR FILE — verify FILE in DIR against DIR/checksums.txt.
gs_verify() {
  local dir="$1" file="$2" line
  line="$(grep -E "  ${file}\$" "${dir}/checksums.txt")" || {
    gs_err "no checksum entry for ${file}"; return 1
  }
  printf '%s\n' "$line" | ( cd "$dir" && gs_sha256 -c - ) >/dev/null
}

gs_main() {
  local arch tmp bindir
  arch="$(gs_arch)" || return 1

  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  gs_err "downloading buzz-fleet (${arch}) from the latest release…"
  gs_fetch "${GS_BASE}/checksums.txt" "${tmp}/checksums.txt" \
    || { gs_err "no published release yet (or network error). See README for releasing."; return 1; }

  gs_fetch "${GS_BASE}/buzz-fleet-${arch}" "${tmp}/buzz-fleet-${arch}" || return 1
  gs_verify "$tmp" "buzz-fleet-${arch}" || { gs_err "checksum mismatch: buzz-fleet-${arch}"; return 1; }

  gs_fetch "${GS_BASE}/buzz-fleet-signer-${arch}" "${tmp}/buzz-fleet-signer-${arch}" || return 1
  gs_verify "$tmp" "buzz-fleet-signer-${arch}" \
    || { gs_err "checksum mismatch: buzz-fleet-signer-${arch}"; return 1; }

  mkdir -p "${GS_PREFIX}/bin"
  install -m 0755 "${tmp}/buzz-fleet-${arch}" "${GS_PREFIX}/bin/buzz-fleet"
  install -m 0755 "${tmp}/buzz-fleet-signer-${arch}" "${GS_PREFIX}/bin/buzz-fleet-signer"

  # Put both on PATH: keep the payload in the data dir, link it into the user bin dir.
  bindir="${XDG_BIN_HOME:-${HOME}/.local/bin}"
  mkdir -p "$bindir"
  ln -sf "${GS_PREFIX}/bin/buzz-fleet" "${bindir}/buzz-fleet"
  ln -sf "${GS_PREFIX}/bin/buzz-fleet-signer" "${bindir}/buzz-fleet-signer"
  gs_err "installed ${GS_PREFIX}/bin/{buzz-fleet,buzz-fleet-signer} → linked into ${bindir}"
  case ":${PATH}:" in
    *":${bindir}:"*) : ;;
    *) gs_err "note: ${bindir} is not on PATH — add it for future shells:"
       gs_err "      echo 'export PATH=\"${bindir}:\$PATH\"' >> ~/.bashrc" ;;
  esac

  # Also link into a root-PATH dir (when sudo permits) so `sudo buzz-fleet …`
  # resolves on distros whose sudo secure_path excludes ~/.local/bin.
  # Best-effort; never fatal.
  if sudo -n true 2>/dev/null; then
    sudo -n ln -sf "${GS_PREFIX}/bin/buzz-fleet" /usr/local/bin/buzz-fleet 2>/dev/null || true
    sudo -n ln -sf "${GS_PREFIX}/bin/buzz-fleet-signer" /usr/local/bin/buzz-fleet-signer 2>/dev/null || true
  fi

  gs_seed_personas "$tmp"

  gs_err "done. Run: buzz-fleet tui"
}

# gs_seed_personas TMPDIR — populate the personas directory with buzz-fleet's
# bundled starter templates, but ONLY on a first install (the directory
# doesn't exist yet). Re-running get.sh to update never touches it again —
# this is a one-time seed, not something that overwrites a user's own
# customizations on every upgrade.
gs_seed_personas() {
  local tmp="$1"
  if [ -e "$GS_PERSONAS_DIR" ]; then
    return 0
  fi
  gs_fetch "${GS_BASE}/personas.tar.gz" "${tmp}/personas.tar.gz" || {
    gs_err "note: couldn't fetch bundled personas (non-fatal) — buzz-fleet works fine without them"
    return 0
  }
  gs_verify "$tmp" "personas.tar.gz" || {
    gs_err "note: bundled personas failed checksum verification (non-fatal), skipping"
    return 0
  }
  mkdir -p "$GS_PERSONAS_DIR"
  tar -xzf "${tmp}/personas.tar.gz" -C "$GS_PERSONAS_DIR"
  gs_err "seeded starter templates into ${GS_PERSONAS_DIR}"
}

# Run only when executed (incl. via `curl | bash`), not when sourced for tests.
if ! (return 0 2>/dev/null); then
  gs_main "$@"
fi
