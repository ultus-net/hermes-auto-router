#!/usr/bin/env bash
# Link both plugin halves into the active Hermes profile and enable the router.
#
#   ./install.sh              # link + enable
#   ./install.sh --no-enable  # link only
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"

mkdir -p "$HERMES_HOME_DIR/plugins/model-providers"

# Model-provider half: discovered by providers/__init__.py, not by PluginManager.
ln -sfn "$HERE/providers/synthetic" "$HERMES_HOME_DIR/plugins/model-providers/synthetic"

# General half: llm_request middleware + `hermes auto-router`.
ln -sfn "$HERE" "$HERMES_HOME_DIR/plugins/auto-router"

echo "linked:"
echo "  $HERMES_HOME_DIR/plugins/model-providers/synthetic -> $HERE/providers/synthetic"
echo "  $HERMES_HOME_DIR/plugins/auto-router              -> $HERE"

if [[ "${1:-}" == "--no-enable" ]]; then
  echo "skipped enable (--no-enable)"
  exit 0
fi

if command -v hermes >/dev/null 2>&1; then
  hermes plugins enable auto-router
else
  echo "hermes not on PATH; enable later with: hermes plugins enable auto-router"
fi
