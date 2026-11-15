#!/usr/bin/env bash
# Install the kiro-microvm skills and agents into ~/.kiro (or $KIRO_HOME).
#
# Usage: scripts/install.sh [--link] [--force] [--dry-run] [--uninstall] [-h|--help]
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [options]

Installs .kiro/skills/* and .kiro/agents/* from this repository into
$KIRO_HOME/skills and $KIRO_HOME/agents (default KIRO_HOME: ~/.kiro).

Options:
  --link       Create symbolic links to the repository files instead of copying.
  --force      Replace existing items with the same name (default: skip them).
  --uninstall  Remove the items this repository provides (matched by name).
  --dry-run    Print what would be done without changing anything.
  -h, --help   Show this help.
EOF
}

mode=copy
force=0
dry_run=0
uninstall=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --link) mode=link ;;
    --force) force=1 ;;
    --dry-run) dry_run=1 ;;
    --uninstall) uninstall=1 ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/.." && pwd -P)"
kiro_home="${KIRO_HOME:-$HOME/.kiro}"

run() {
  if [ "$dry_run" -eq 1 ]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

installed=0
skipped=0
removed=0

# process_item <src> <dest_dir>
process_item() {
  local src="$1" dest_dir="$2" name dest
  name="$(basename -- "$src")"
  dest="$dest_dir/$name"

  if [ "$uninstall" -eq 1 ]; then
    if [ -e "$dest" ] || [ -L "$dest" ]; then
      printf 'Removing %s\n' "$dest"
      run rm -rf -- "$dest"
      removed=$((removed + 1))
    else
      printf 'Not installed, skipping: %s\n' "$dest"
    fi
    return 0
  fi

  if [ -e "$dest" ] || [ -L "$dest" ]; then
    if [ "$force" -eq 0 ]; then
      printf 'Already exists, skipping (use --force to replace): %s\n' "$dest"
      skipped=$((skipped + 1))
      return 0
    fi
    printf 'Replacing %s\n' "$dest"
    run rm -rf -- "$dest"
  fi

  if [ "$mode" = link ]; then
    printf 'Linking %s -> %s\n' "$dest" "$src"
    run ln -s -- "$src" "$dest"
  else
    printf 'Copying %s -> %s\n' "$src" "$dest"
    run cp -R -- "$src" "$dest"
  fi
  installed=$((installed + 1))
}

for kind in skills agents; do
  src_dir="$repo_root/.kiro/$kind"
  dest_dir="$kiro_home/$kind"
  if [ ! -d "$src_dir" ]; then
    printf 'Source directory not found, skipping: %s\n' "$src_dir" >&2
    continue
  fi
  if [ "$uninstall" -eq 0 ]; then
    run mkdir -p -- "$dest_dir"
  fi
  for src in "$src_dir"/*; do
    [ -e "$src" ] || continue
    process_item "$src" "$dest_dir"
  done
done

if [ "$uninstall" -eq 1 ]; then
  printf 'Done. Removed: %d\n' "$removed"
  exit 0
fi

printf 'Done. Installed: %d, skipped: %d (KIRO_HOME=%s)\n' "$installed" "$skipped" "$kiro_home"

if ! command -v kiro-microvm >/dev/null 2>&1; then
  cat <<'EOF'

'kiro-microvm' was not found on PATH. Install the CLI with pipx:

  pipx install ./packages/runs-cli

(run this from the repository root; use 'pipx install --force' to reinstall)
EOF
fi
