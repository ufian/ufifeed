#!/usr/bin/env bash
# Deploy only committed files; account credentials never leave VM state/.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Commit all changes before deploying." >&2
  exit 1
fi
revision="$(git rev-parse HEAD)"
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid Git revision" >&2
  exit 1
fi
host="ufian@192.168.1.99"
base="/home/ufian/ufifeed"
git archive --format=tar HEAD | /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" \
  "mkdir -p '$base/releases/$revision' && tar -xf - -C '$base/releases/$revision'"
/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- "$revision" <<'REMOTE'
set -euo pipefail
revision="$1"
base="/home/ufian/ufifeed"
release="$base/releases/$revision"
umask 077
mkdir -p "$base/state/config" "$base/state/data" "$base/state/secrets"
cp "$release/config/ufifeed.example.toml" "$base/state/config/ufifeed.example.toml"
cd "$release"
printf 'UFIFEED_STATE_DIR=%s\nUFIFEED_IMAGE_TAG=%s\n' "$base/state" "$revision" > .env
docker compose config --quiet
docker compose build app
docker run --rm "ufifeed:$revision" --config config/ufifeed.example.toml check
if [[ -f "$base/state/config/ufifeed.toml" && -f "$base/state/secrets/credentials.json" \
   && -f "$base/state/secrets/reader.session" && -f "$base/state/secrets/data.key" ]]; then
  docker compose up -d app
  docker compose ps
else
  echo "Image ready. Initial setup and Telegram authorization are still required."
fi
ln -sfn "$release" "$base/current"
echo "Release prepared: $revision"
REMOTE
