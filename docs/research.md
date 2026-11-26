# Research

Checked on **2026-11-25**. The user scenario is mainly third-party channels/groups where our bot cannot be added. The subsequent implementation uses a dedicated reader account and a separate delivery bot.

## Access options

| Approach | Access and recovery | Fit |
| --- | --- | --- |
| Bot API | Chats/channels accessible to the bot; group privacy mode matters; updates expire after 24 hours; no general history-reading endpoint | Delivery, controls, and sources owned by the user |
| User MTProto client | Content accessible to an authorized account; available history and live events | Main approach for third-party sources |
| TDLib | Full client state, networking, ordered updates, native integration | Alternative if a fuller client becomes necessary |
| Public `t.me/s/` HTML | Only some public content; not a reliable collection API contract | Not a foundation for this service |
| Manual forwards | Only messages explicitly submitted to the bot | Useful manual prototype, not automatic monitoring |

Primary references: [Bot FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get), [Bot API updates](https://core.telegram.org/bots/api#getting-updates), [Telethon client reference](https://docs.telethon.dev/en/stable/modules/client.html), [TDLib](https://core.telegram.org/tdlib). Suitability assessments are project decisions.

The user client reads as the dedicated account; the bot publishes digests. Admin rights in third-party sources are unnecessary when ordinary membership provides reading access. Private sources require that account's legitimate access. Folder membership does not automatically join chats. The MVP intentionally excludes secret chats, access bypasses, bulk platform archiving, media downloads, and automatic joins.

## Libraries

**Python + Telethon + aiogram** keeps the collector, controls, and scheduler in one asyncio process. Dependencies are pinned in `uv.lock`; current Telegram compatibility still needs verification on the actual sources.

- [Telethon](https://docs.telethon.dev/en/stable/) supplies MTProto history and events.
- [aiogram](https://docs.aiogram.dev/en/latest/) supplies Bot API controls and delivery.
- [TDLib](https://core.telegram.org/tdlib) is the official full-client alternative, with a native integration cost.
- [Pyrogram](https://docs.pyrogram.org/) is not selected because its documentation says it is no longer maintained.

## Authentication and recovery

Create your own application credentials using [Telegram API development tools](https://core.telegram.org/api/obtaining_api_id). These identify the client application and are separate from the bot token. A user account then authorizes the reader session.

A [Telethon session](https://docs.telethon.dev/en/stable/concepts/sessions.html) contains reusable login credentials. Keep it private and outside Git/images/logs; revoke the relevant Telegram session if compromised. A dedicated account reduces the blast radius but must obtain source access independently. The session itself is not read-only: application behavior provides that boundary.

Telegram chooses code delivery, including its service chat and email. SMS is not guaranteed for third-party clients, and forcing SMS is not a reliable solution. [User authorization](https://core.telegram.org/api/auth) and [QR login](https://core.telegram.org/api/qr-login) document these options. The implementation supports QR approval from an already signed-in reader phone and explains the selected code route.

Honor server-directed [FloodWait](https://docs.telethon.dev/en/stable/concepts/errors.html), preserve progress, and limit concurrency. There is no promised universal safe number of channels/requests. [Deletion events](https://docs.telethon.dev/en/stable/modules/events.html#telethon.events.messagedeleted.MessageDeleted) are not guaranteed for every case, so the service cannot promise a perfect Telegram mirror.

## Defining importance

A personal interest profile is more useful than popularity alone: topics, exclusions, source scope, and recency. Reactions/views/replies can be future signals where available; they are not equivalent to personal relevance. Evaluate the digest on a small owner-labeled sample to identify useful inclusions and missed items.

Extractive mode uses deterministic topic filters, duplicate removal, short source excerpts, and links. It is useful without an AI provider, but does not translate content or semantically merge differently worded reports.

AI mode summarizes selected messages, groups repeated claims, and must preserve uncertainty and source references. The MVP is a single bounded request, not a full discussion/reply-chain analysis system. Input remains untrusted data; the model receives no authorization to operate Telegram or the host.

## AI terms and providers

[Telegram API terms §1.5](https://core.telegram.org/api/terms) incorporate [content licensing and AI terms](https://telegram.org/tos/content-licensing). Their language extends beyond model training to AI deployment. Exceptions may be granted with explicit, informed, continuing consent from all relevant users within the specified context. A subscription, public channel, or local model is not automatically such permission. This is a project risk assessment, not a legal conclusion; establish an appropriate basis for each AI-enabled source.

The implementation keeps AI off by default and requires explicit source opt-in before either provider receives content. Opting out stops future requests and invalidates pending digest payloads. Ordinary excerpts must also respect applicable content rights.

Supported providers:

- OpenAI-compatible Chat Completions, with server-side endpoint/model/key configuration.
- Authenticated Codex CLI, explicitly requested by the user. The [official headless auth guide](https://developers.openai.com/codex/auth/) documents auth-cache transfer; [non-interactive execution](https://developers.openai.com/codex/noninteractive/) documents structured outputs and account-backed automation. Runs are ephemeral and text-only, with tools disabled and auth stored privately.

Client digest language is selected with `/language`; English application code does not imply English-only output. The first deployment uses the owner's requested/conversational preference, and subsequent changes are made in the bot.

HTTP costs depend on input/output tokens and the chosen provider's rates. Codex uses the signed-in account's available limits. The app caps input size and daily attempts, not provider currency spend. Provider errors are visible and do not silently switch backends.

## Delivery and MVP scope

The owner opens/pairs the bot before delivery. Long polling works behind NAT without inbound HTTPS. [sendMessage](https://core.telegram.org/bots/api#sendmessage) limits text to 4096 characters after entity parsing; the application uses a conservative 3500 UTF-16-unit HTML bound, escapes text, and persists part-level send state. Rate-limit rejections honor [retry_after](https://core.telegram.org/bots/api#responseparameters).

Construct original-message URLs using [Telegram deep-link formats](https://core.telegram.org/api/links). Private links still require destination-account membership; ordinary groups may have no universal link. Private source material is delivered only to the paired owner's private chat.

One Docker app with SQLite is sufficient for the initial personal workload. Redis, Celery, Kubernetes, a vector database, a web UI, video processing, and a local large model are not needed for this MVP. The [architecture](architecture.md) documents actual guarantees and remaining limits.
