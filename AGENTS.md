# Project conventions

- Write source code, identifiers, comments, documentation, CLI messages, and bot controls in English.
- Digest language is a client preference selected in the bot with `/language`, persisted in SQLite.
  Do not hardcode the summary language in AI instructions or server configuration.
- Keep translated digest labels in `src/ufifeed/locales/*.json`; non-English localization data
  belongs there, not in application logic. Extractive excerpts retain their original language.
- The reader account and the bot owner are separate Telegram accounts. Read only explicitly
  selected channels/groups from the reader's folder; accept controls only in the owner's private chat.
- Never commit credentials, sessions, encryption keys, collected messages, or runtime databases.
- Use `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest` for changes.
- Commit and push requested code changes. The VM is `ufian@192.168.1.99`; use the deployment
  script and `/home/ufian/ufifeed/compose` to resolve the current release reliably.
