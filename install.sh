#!/usr/bin/env bash
# Link both plugin halves into the active Hermes profile and enable the router.
#
# Works from either location:
#   * a git clone       -> links the clone as the general plugin
#   * an installed copy -> `hermes plugins install` already placed the general
#                          half; this only links the provider half
#
#   ./install.sh              # link + enable
#   ./install.sh --no-enable  # link only
#   ./install.sh --help
set -euo pipefail

usage() {
  cat <<'EOF'
usage: ./install.sh [--no-enable]

  --no-enable  Link both halves but do not run `hermes plugins enable`.
  -h, --help   Show this message.
EOF
}

enable=1
if [[ $# -gt 1 ]]; then
  echo "too many arguments" >&2
  usage >&2
  exit 2
fi
case "${1:-}" in
  "") ;;
  --no-enable) enable=0 ;;
  -h|--help) usage; exit 0 ;;
  *)
    echo "unknown option: $1" >&2
    usage >&2
    exit 2
    ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
GENERAL_TARGET="$HERMES_HOME_DIR/plugins/auto-router"
PROVIDER_TARGET="$HERMES_HOME_DIR/plugins/model-providers/synthetic"

# Link one directory into place. A real directory that is already the same
# tree is left alone; any other real path is never replaced.
link_dir() {
  local src="$1"
  local dest="$2"
  local label="$3"

  if [[ -L "$dest" ]]; then
    ln -sfn "$src" "$dest"
    echo "linked $label: $dest -> $src"
    return
  fi
  if [[ -d "$dest" ]]; then
    if [[ "$(cd "$dest" && pwd -P)" == "$(cd "$src" && pwd -P)" ]]; then
      echo "$label already in place: $dest"
    else
      echo "error: $dest exists and is not a symlink; refusing to replace it" >&2
      return 1
    fi
    return
  fi
  if [[ -e "$dest" ]]; then
    echo "error: $dest exists and is not a directory; refusing to replace it" >&2
    return 1
  fi
  ln -sfn "$src" "$dest"
  echo "linked $label: $dest -> $src"
}

mkdir -p "$HERMES_HOME_DIR/plugins/model-providers"

# Model-provider half. Hermes discovers this from providers/__init__.py, not
# from PluginManager, so `hermes plugins install` cannot place it: it has to
# live under plugins/model-providers/<name>/.
link_dir "$HERE/providers/synthetic" "$PROVIDER_TARGET" "provider"

# General half (middleware + `hermes auto-router`). Running from the installed
# copy is the normal `hermes plugins install` case, so the tree is already there.
link_dir "$HERE" "$GENERAL_TARGET" "general plugin"

if [[ "$enable" -eq 0 ]]; then
  echo "skipped enable (--no-enable)"
  exit 0
fi

if command -v hermes >/dev/null 2>&1; then
  hermes plugins enable auto-router
else
  echo "hermes not on PATH; enable later with: hermes plugins enable auto-router"
fi
