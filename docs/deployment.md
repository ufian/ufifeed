# Deployment and operations

## VM

Verified on 2026-11-25: `ufian@192.168.1.99`, Ubuntu 26.04 LTS, x86_64, Docker 29.5.2, Compose v5.1.4. RAM: 7.3 GiB total, about 5 GiB available at inspection; root disk: 98 GiB total, 77 GiB free. User UID/GID is 1000. Other projects already run here; ufifeed never restarts Docker globally.

## Releases

```sh
./scripts/deploy.sh
```

The script requires a clean tree, transfers committed files using `git archive | ssh`, builds `ufifeed:<SHA>`, validates Compose, and runs a config smoke check. It updates an already running main app, but leaves a stopped app stopped. An unauthenticated `.session` file does not prove successful login. One-off auth containers do not trigger automatic startup.

```text
/home/ufian/ufifeed/
  compose                   # stable launcher resolving current on every invocation
  current -> releases/<SHA>
  releases/<SHA>/            # code and a generated .env with paths/image tag
  state/
    config/ufifeed.toml
    data/ufifeed.sqlite3
    secrets/credentials.json
    secrets/reader.session
    secrets/data.key
/home/ufian/.codex/auth.json # private Codex account auth, mounted only at runtime
```

Use `/home/ufian/ufifeed/compose`, even from an old SSH shell: changing a symlink does not change a shell's existing working directory. The launcher ignores stale image/project environment overrides and selects the release's own `.env` and Compose file.

The app runs as UID/GID 1000 with a read-only root, tmpfs `/tmp`, dropped capabilities, no Docker socket, and no published ports. Runtime paths and the private Codex home are writable mounts. Initial resource budgets are 1 CPU and 1 GiB; measure real workload before changing them.

## Authorization and bootstrap

See [README](../README.md) for `setup`, `auth --qr`, and startup commands. Secrets and 2FA are entered in your terminal, not in chat or shell arguments. Setup will not overwrite existing configuration. QR login must be approved by a signed-in phone using the **reader account**.

The CLI reports code delivery methods and server-directed waits without repeated automatic requests. `Ctrl+C` cancels cleanly and restores terminal echo. `Ctrl+Z` suspends the Docker client; it does not terminate a waiting login container.

For Codex, `scripts/install_codex.py` installs version 0.172.0 from the official release and verifies its pinned SHA-256. Headless login can use device authentication or a securely transferred existing auth cache, as described in the [official auth guide](https://developers.openai.com/codex/auth/). Auth files remain private (0600) and are never committed. `codex login status` verifies the selected sign-in method; a real synthetic request verifies usable model access.

Once the AI provider is configured, the operator can choose `Ufifeed`, set Russian as the client preference, opt its current sources into AI, and queue one initial digest using `configure` (see README). The main app must be stopped while that command uses the reader session. Start the app afterwards; the delivery worker sends the persisted outbox to the paired primary account.

## Status

```sh
/home/ufian/ufifeed/compose ps
/home/ufian/ufifeed/compose logs --tail 100 app
/home/ufian/ufifeed/compose run --rm --interactive=false -T app status
```

`status` reports folder, language, mode, source/message counts, errors, and delivery counts without text or credentials. The bot additionally shows source issues and schedule settings. Docker health only verifies the event-loop/SQLite heartbeat, not every external source. Quiet channels are not considered broken.

Unexpected critical-task exit terminates the process so `restart: unless-stopped` can recover it. Docker does not automatically restart an unhealthy container. Reauthorize a revoked session after stopping the app. Logs rotate at 3 × 10 MiB; network libraries are silenced to avoid leaking tokens from URLs or message content from errors.

## Backup and recovery

Stop the app and create a consistent database backup inside a writable mount:

```sh
/home/ufian/ufifeed/compose stop app
/home/ufian/ufifeed/compose run --rm app backup data/backup.sqlite3
```

The command uses SQLite's backup API and refuses to overwrite an existing destination. Copy the backup with matching server config, Telegram secrets, data key, and needed Codex auth to a separate encrypted private backup store. Session/auth files grant account access. The database text cannot be recovered without its matching data key. External backup storage/automation is not configured by this project.

Restart with `compose up -d app`. Remove expired backup copies independently; application retention does not purge backups. Seven days is a suggested backup lifetime, not an installed policy.

For restore, stop the app, preserve current state, restore a consistent version of database/config/secrets with UID/GID 1000 and restricted permissions, and start a compatible image. Never run two reader instances. An old outbox backup may predate real Telegram sends; check the chat before retrying uncertain deliveries.

## Updates and rollback

Back up before future schema migrations. Schema version 1 is current. Roll back code only with a compatible database, or restore the matching snapshot. Releases retain their image tag and state paths in `.env`; start Compose from the chosen release and update `current` after a successful rollback. No registry is required.

Never use `down -v`, global prune, or restart the Docker daemon to update ufifeed. Old releases/images are retained for deliberate cleanup and rollback.
