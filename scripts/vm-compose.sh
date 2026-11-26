#!/usr/bin/env bash
# Always resolve the current release, even from a shell opened before deployment.
set -euo pipefail
release="$(readlink -f /home/ufian/ufifeed/current)"
unset UFIFEED_IMAGE_TAG UFIFEED_STATE_DIR COMPOSE_FILE COMPOSE_PROJECT_NAME
exec docker compose --env-file "$release/.env" --project-directory "$release" \
  -f "$release/compose.yaml" "$@"
