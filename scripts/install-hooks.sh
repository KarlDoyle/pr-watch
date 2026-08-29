#!/usr/bin/env bash
# uv-equivalent of npm's `prepare`: point git at the committed hooks dir so the
# pre-push gate runs. Run this once after `uv sync`.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath .githooks
echo "hooks: core.hooksPath -> .githooks (pre-push gate active)"
