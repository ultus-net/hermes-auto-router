#!/usr/bin/env bash
# Link both plugin halves into the active Hermes profile and enable the router.
#
# Works from either location:
#   * a git clone            -> links the clone as the general plugin
#   * an installed copy      -> `hermes plugins install` already placed the
#                               general half; this only links the provider half
#
#   ./install.sh              # link + enable
#   ./install.sh --no-enable  # link only
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
GENERAL_TARGET="$HERMES_HOME_DIR/plugins/auto-router"
PROVIDER_TARGET="$HERMES_HOME_DIR/plugins/model-providers/synthetic"

mkdir -p "$HERMES_HOME_DIR/plugins/model-providers"

# Model-provider half. Hermes discovers this from providers/__init__.py, not
# from PluginManager, so `hermes plugins install` cannot place it: it has to
# live under plugins/model-providers/<name>/.
ln -sfn "$HERE/providers/synthetic" "$PROVIDER_TARGET"
echo "linked provider: $PROVIDER_TARGET -> $HERE/providers/synthetic"

# General half (middleware + `hermes auto-router`). Skip when this script is
# already running from the installed location, and never clobber a real
# directory with a symlink.
if [[ -d "$GENERAL_TARGET" && ! -L "$GENERAL_TARGET" ]]; then
  if [[ "$(cd "$GENERAL_TARGET" && pwd -P)" == "$HERE" ]]; then
    echo "general plugin already in place: $GENERAL_TARGET"
  else
    echo "warning: $GENERAL_TARGET exists and is not a symlink; leaving it alone" >&2
  fi
else
  ln -sfn "$HERE" "$GENERAL_TARGET"
  echo "linked general:  $GENERAL_TARGET -> $HERE"
fi

if [[ "${1:-}" == "--no-enable" ]]; then
  echo "skipped enable (--no-enable)"
  exit 0
fi

if command -v hermes >/dev/null 2>&1; then
  hermes plugins enable auto-router
else
  echo "hermes not on PATH; enable later with: hermes plugins enable auto-router"
fi
