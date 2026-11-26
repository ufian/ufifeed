# ufifeed

Private Telegram digests from channels and groups selected in a dedicated reader account folder. A separate bot delivers summaries to your primary account and accepts controls only in that account's private chat.

Application code, comments, documentation, CLI messages, and bot controls are in English. **Digest language is a client preference**, selected in the bot using `/language en` or `/language ru` and persisted in SQLite. AI summaries follow that preference; extractive excerpts retain the source language.

## Features

- Folder selection through `/folders` and `/folder ID`; membership reconciliation every five minutes and before each digest.
- Encrypted message journal with observed edit versions, deletion tombstones, reaction changes, reply/topic references and media metadata. Initial import covers the latest 24 hours; live collection continues afterwards.
- One cross-channel synthesis of important world events, plus a separate discussion summary for every group, with attention to reactions and deleted content. Topic/exclusion filters and source links.
- AI summaries through authenticated **Codex CLI** or an OpenAI-compatible Chat Completions endpoint, with per-source AI access and citation validation.
- Daily schedules in an IANA time zone, including Seattle (`America/Los_Angeles`), manual previews, pause/resume, and missed-window coalescing.
- Encrypted message/outbox payloads, persistent SQLite state, and explicit handling of uncertain deliveries.
- One Docker Compose application, CI, and tests; no inbound ports required.

Only explicitly selected channels and groups are supported, including pinned peers. Personal dialogs and bots are skipped. Automatic categories and unread/muted/archived filters are rejected. Protected/disappearing content is excluded; media files are not downloaded. Telegram does not guarantee every deletion event. The collector also rechecks the latest 200 saved IDs and rotates through 100 older IDs per source on each synchronization. It never interprets numeric history gaps as deletions. Only versions actually observed can be archived: content deleted before first capture and edits overwritten before observation cannot be recovered. Protected/disappearing content is excluded even from version history.

## VM deployment

Run from a clean, committed checkout:

```sh
./scripts/deploy.sh
```

Target: `ufian@192.168.1.99`. Releases are stored under `/home/ufian/ufifeed/releases/<SHA>`; runtime state stays in `/home/ufian/ufifeed/state`. Running services are updated; a stopped service remains stopped. The image includes a pinned, checksum-verified Linux x86_64 Codex binary.

**Use the stable VM launcher** so a shell opened before deployment cannot accidentally select an old release:

```sh
/home/ufian/ufifeed/compose run --rm app setup
/home/ufian/ufifeed/compose run --rm app auth --qr
/home/ufian/ufifeed/compose up -d app
```

Run these in your own SSH terminal (`ssh -t ufian@192.168.1.99`). `setup` requests your application's `api_id`/`api_hash` from [my.telegram.org](https://my.telegram.org) and a **new dedicated bot token** from [BotFather](https://t.me/BotFather). It then prints a one-time command to send to the bot from your primary account. Credentials are stored privately on the VM.

For QR login, switch to the **reader account** on your phone, open **Settings → Devices → Link Desktop Device**, scan the terminal QR, and approve login. Two-step verification, if enabled, is requested with hidden input. QR codes refresh on expiry. Do not share a live login QR.

For phone-code login, omit `--qr`. The CLI explains the delivery method selected by Telegram: the code may arrive in Telegram's service chat on already signed-in reader devices, not by SMS. Avoid repeated requests. `Ctrl+C` cancels login; `Ctrl+Z` only suspends the terminal command. Never run two processes against the same reader session.

## Bot configuration

Open the bot from your primary account:

```text
/start
/folders
/folder 2
/language ru
/topics release, security, update
/exclude #ad, promotion
/schedule 09:00,20:00 America/Los_Angeles
/digest
/status
```

Folder IDs, topics, and times above are examples. Create and maintain the folder in the reader account's Telegram app. The bot settings survive image updates. Your primary account does not need to join every source to receive a digest, but it does need source access to open private message links.

Useful controls:

- `/language` shows available digest languages and the current preference.
- `/mode extractive` selects source excerpts; `/mode ai` selects AI summaries.
- `/ai_source ID on|off` controls a source. `/ai_folder on|off` controls current and future members of the selected folder; the default is off.
- `/journal` shows archive counts. `/history SOURCE_ID MESSAGE_ID` returns observed text versions and their timestamps, including deleted messages.
- `/pause` stops scheduled digests while collection continues; `/resume` resumes and can catch up.
- `/retry ID` explains the possible duplicate risk before an uncertain delivery can be retried.
- `/purge ID` removes retained messages for a disabled source and stored digest payloads. Delivered Telegram messages and external backups are unaffected.

## Mini App

The owner-only Telegram Mini App provides briefings, a searchable-by-source message journal with observed versions, source controls and client preferences. Open the **Ufifeed** menu button in the bot. The public endpoint is `https://ufifeed.ufian.com:8443/`; private APIs require signed Telegram owner authorization. The VM remains the only data store behind a restricted SSH tunnel and the shared HTTPS proxy. See [Mini App deployment](docs/miniapp.md).

Summary titles are bold; details and references are collapsible. Group summaries keep only 1–3 important topics with 1–2 short sentences per topic, without introductory filler.

## AI backends

Set server-side `[ai]` parameters in `state/config/ufifeed.toml`, then recreate the application. Provider credentials are not accepted through bot commands.

For the ChatGPT account authenticated in Codex:

```toml
[ai]
enabled = true
provider = "codex"
model = "" # Use the account's default Codex model, or specify one explicitly.
```

The Compose launcher mounts the VM's private Codex home at `/app/codex`. CLI runs are ephemeral, use a temporary working directory, ignore user/project configuration, disable shell/browser/plugins and other tools, and request schema-constrained JSON. Tokens never go into images or Git. This backend uses the authenticated account's Codex limits; the app also limits daily attempts.

For a compatible HTTP endpoint:

```toml
[ai]
enabled = true
provider = "chat-completions"
base_url = "https://api.openai.com/v1"
model = "your-model"
```

Store `ai_api_key` in `state/secrets/credentials.json` or set `AI_API_KEY`. A local endpoint may not require a key. Then enable AI access for the intended sources and run `/mode ai`.

Every eligible message in the window enters bounded batches, including duplicates and retained deleted posts. Long text is split, never silently cropped. Intermediate findings are cached encrypted and merged recursively into the final synthesis. Daily attempt limits and provider limits still apply; incomplete preparation does not advance the window or send a partial result. `/status` reports progress.

Only AI-enabled sources cross either backend boundary. Citation IDs and output structure are checked, but that does not prove the model's factual accuracy. Provider errors retain collected data and leave scheduled windows unadvanced. There is no silent fallback to another provider. Telegram's [content licensing terms](https://telegram.org/tos/content-licensing#large-language-models-and-ai) restrict AI use more broadly than training; see [research](docs/research.md) before enabling sources.

## Offline bootstrap and operations

Stop the main app before using the reader session from an administrative command. The following explicitly chooses a folder, enables AI for its current and future sources, and queues one initial digest for delivery when the app starts:

```sh
/home/ufian/ufifeed/compose stop app
/home/ufian/ufifeed/compose run --rm app configure --folder Ufifeed --language ru --mode ai --enable-ai-for-folder --queue-digest
/home/ufian/ufifeed/compose up -d app
/home/ufian/ufifeed/compose run --rm --interactive=false -T app status
```

The initial digest key is idempotent per folder. Use `/digest` for subsequent manual previews, or `configure --folder Ufifeed --queue-preview` while the app is stopped. `status` reports counts and delivery states without message bodies or credentials.

The message journal is retained indefinitely by default (`storage.retention_days = 0`). Positive values opt into finite retention of messages and their versions. Existing installations keep their explicit setting until changed. Derived AI cache/outbox payloads expire after 30 days with unlimited archive retention. Message bodies, version snapshots, reaction/sender details and AI cache/outbox payloads use Fernet; IDs, titles, times, deletion flags and settings remain readable metadata. Session/auth cache files rely on filesystem permissions. Do not lose `state/secrets/data.key`: retained text cannot be recovered without it.

## Development

Python 3.12–3.14 is supported; 3.13 is recommended. Dependencies are pinned in `uv.lock`.

```sh
uv sync --frozen --python 3.13
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run ufifeed --config config/ufifeed.example.toml check
```

For local development, create `data/` and `secrets/`, then use `uv run ufifeed setup`, `auth`, and `run`. For local Compose, also provide a private `codex/` directory or set `UFIFEED_CODEX_HOME`. Never share a bot polling token or reader session between simultaneous instances.

[Architecture and limits](docs/architecture.md) · [Deployment, backup, recovery](docs/deployment.md) · [Research](docs/research.md)
