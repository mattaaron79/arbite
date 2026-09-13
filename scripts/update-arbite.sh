#!/usr/bin/env bash
#
# Reinstall arbite from this repo checkout into the global pipx environment.
# Safe to run from any directory: the repo root is derived from this script's
# own location (this file lives in <repo>/scripts/), not from $PWD.
#
# Do NOT run this with sudo. pipx installs per-user, so under sudo the package
# lands in /root/.local and stays invisible to your normal shell.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname -- "$script_dir")"

if [ "$(id -u)" -eq 0 ]; then
    {
        echo "Refusing to run as root/sudo: pipx would install into root's"
        echo "environment (/root/.local), not the account that owns this checkout."
        echo "Re-run without sudo, e.g.: ./scripts/update-arbite.sh"
    } >&2
    exit 1
fi

# Resolve how to invoke pipx. `python` is not guaranteed to exist on Linux
# (Debian/Ubuntu ship `python3` only), so prefer a real `pipx` on PATH.
if command -v pipx >/dev/null 2>&1; then
    pipx_cmd=(pipx)
elif command -v python3 >/dev/null 2>&1; then
    pipx_cmd=(python3 -m pipx)
elif command -v python >/dev/null 2>&1; then
    pipx_cmd=(python -m pipx)
else
    echo "error: none of 'pipx', 'python3' or 'python' were found on PATH." >&2
    exit 1
fi

cd -- "$repo_root"

echo "Updating arbite (global pipx install) from $PWD ..."
echo

if ! "${pipx_cmd[@]}" install "." --force; then
    echo
    echo "Update failed - see errors above." >&2
    exit 1
fi

echo
echo "Done."

# Report the version from the pipx bin dir rather than relying on PATH: pipx may
# have just installed the app without its bin dir being on PATH yet.
pipx_bin_dir="$("${pipx_cmd[@]}" environment --value PIPX_BIN_DIR 2>/dev/null || true)"
if [ -n "$pipx_bin_dir" ] && [ -x "$pipx_bin_dir/arbite" ]; then
    "$pipx_bin_dir/arbite" --version
elif command -v arbite >/dev/null 2>&1; then
    arbite --version
else
    echo "Installed, but 'arbite' is not on your PATH yet." >&2
    echo "Run 'pipx ensurepath' and restart your shell." >&2
fi
