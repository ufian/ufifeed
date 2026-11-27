# Telegram Mini App

Open the **Ufifeed** menu button in the paired owner's private bot chat. The interface is English; summary language is the persisted `/language` preference, also editable in Settings.

- **Briefings:** recent summaries, preparation progress and a new-preview button. Individual titles are bold; details and source links start collapsed. Group discussions have at most three important topics with one or two short sentences each.
- **Journal:** all retained selected-source messages, a deleted-only filter and pagination. Versions show the observed edit history and deletion record. Text is rendered as text, never executable HTML.
- **Sources:** choose the reader folder, inspect synchronization and change per-source/folder AI access.
- **Settings:** language, mode, schedule, timezone and topic/exclusion filters. Revision checks prevent silently overwriting concurrent bot changes. Changing times/zone starts a new window; pausing preserves the existing boundary.

The app never asks for a phone number, Telegram login code, bot token or AI credentials. Those remain server-side. Direct browser visits show a public shell with no private data. The backend validates the raw Telegram `initData` HMAC, a one-hour age limit, future-clock tolerance and the paired numeric owner ID. Duplicate fields, malformed users and other accounts are rejected. Auth travels only in an Authorization header, never a URL or browser storage. Responses use `no-store`, a restrictive CSP and no third-party analytics.

## Network layout

```text
Telegram -> https://ufifeed.ufian.com:8443/
         -> nginx on 96.44.158.249
         -> 127.0.0.1:18081 (restricted reverse SSH listener)
         -> VM 127.0.0.1:8082 -> the existing Ufifeed application
```

The app runs in the same event loop as collection, bot controls and the digest lock, with the same SQLite/key. Only the VM keeps the journal, Telegram credentials and Codex auth. The public host is a forwarding proxy with no message database or application credentials. Requests necessarily pass through its TLS proxy in memory; request access logging, disk buffering and proxy caching are disabled. The app has no local storage or service worker.

The existing pplan nginx proxy serves both applications on 8443 using separate domain/SNI virtual hosts and certificates. Its calendar route still uses 18080; Ufifeed uses 18081. Port 443 remains with the existing VPN. No inbound VM router port is required.

## Certificate and public proxy

DNS A for `ufifeed.ufian.com` points to `96.44.158.249`. Issue a separate Let's Encrypt certificate using the same pinned Certbot container and HTTP-01 webroot as pplan:

```sh
docker run --rm --network host \
  -v /etc/letsencrypt:/etc/letsencrypt \
  -v /var/www/pplan-acme:/var/www/acme \
  certbot/certbot@sha256:c5698da859429ccee0fcc299a8000368198222d1f9e789ab6e388b491e8ce06d \
  certonly --non-interactive --agree-tos --register-unsafely-without-email \
  --webroot -w /var/www/acme --cert-name ufifeed.ufian.com -d ufifeed.ufian.com
```

The current default port-80 virtual host already serves that ACME webroot. Back up `/opt/pplan-proxy/nginx.conf`, then insert [ufifeed-nginx.conf](../deploy/ufifeed-nginx.conf) **inside** its existing `http {}` block, retaining pplan's configuration. Write to the existing file inode because it is bind-mounted. Run `docker exec pplan-proxy nginx -t` before `nginx -s reload`; restore the backup on validation failure. Reload does not restart the calendar or VPN.

The existing `pplan-cert-renew.timer` runs `certbot renew` for all certificates in `/etc/letsencrypt`, including Ufifeed, then reloads the shared proxy. Use the same mounted webroot for renewal. Verify the timer and a dry-run renewal for `--cert-name ufifeed.ufian.com`. Do not create a competing proxy on the same ports.

## Restricted tunnel

Create a dedicated Ed25519 key on the VM at `state/secrets/miniapp_tunnel_key` (0600). Only its public key goes to the public host's `ufifeed-tunnel` account. Restrict that account to remote forwarding at `127.0.0.1:18081`: no shell, TTY, local forwarding, agent, X11 or password login. Use both `authorized_keys` restrictions and an sshd `Match User` block; validate sshd before reloading it.

Pin the public host's verified Ed25519 key in `state/secrets/miniapp_known_hosts`, using an existing trusted SSH connection as the verification channel. Install [ufifeed-tunnel.service](../deploy/ufifeed-tunnel.service) as a VM system service and enable it. The private tunnel key is never copied to the proxy or repository. The service reconnects and starts after reboot.

## VM configuration

In persistent `state/config/ufifeed.toml`:

```toml
[miniapp]
enabled = true
public_url = "https://ufifeed.ufian.com:8443/"
port = 8082
```

Deploy with `scripts/deploy.sh`. Compose publishes 8082 only on VM loopback. If changing the internal port, update both Compose and the tunnel mapping. Startup configures the owner's private menu button automatically using the existing bot token. An optional Main App/profile button can use the same URL in BotFather.

The initial schedule remains disabled until the owner selects delivery times. Full-window multi-stage AI analysis uses multiple requests per digest; the default daily attempt cap is 100. Provider account limits also apply. Failed analysis retains messages/cache and does not enqueue partial summaries or advance the window. History requests are paced, unchanged snapshots are not rewritten, and overlap pages commit together to keep HTTP responsive. A Telegram cooldown is persisted and displayed during preparation; short pauses are retried automatically.

## Verification and recovery

Run the project checks and HTTP tests. Browser QA uses a temporary local server with synthetic messages and synthetic Telegram signatures; never add an authentication bypass to the production app. Check public `/health`, an unsigned `/api/state` (401) and a signed owner request without logging its header or private response. Check `ufifeed-tunnel`, Docker health, certificate expiry/renewal and the existing pplan health endpoint after proxy changes.

Back up the VM's encrypted database with its matching key. Replacing the public proxy cannot erase the journal; restore its certificate/configuration/tunnel public key and DNS, then reconnect the VM tunnel. If the VM or tunnel is down, the UI reports a connection error rather than displaying a stale private cache.

## Shared digests

In the owner's private bot chat, run `/sources` to see source IDs. Use `/invite ID [ID ...]` to grant access to particular sources, or `/invite all` to grant access to the sources currently in the reader folder. Forward the resulting message to one person. It includes a link and instructions for joining. The recipient can open the link and tap **Start**, or send the included `/join CODE` command in the bot’s private chat. Both methods use the same single-use code, which expires after 24 hours.

Each subscriber receives a digest in their own bot chat on the shared delivery schedule. **Digest settings** and `/subscribe` select from that subscriber's granted active sources. `/language en|ru` changes their digest language. The owner can also choose their own sources using the settings link. Subscriber digests use the owner’s collection and summarization policies and only include posts from their subscription period. New reader sources require a fresh invitation to become available to an existing subscriber.

Use `/subscribers` to list user IDs and `/revoke USER_ID` to revoke access and drop queued deliveries for that user. Subscribers can leave using `/unsubscribe`. Forward a channel post to the owner’s bot chat to add its channel to the selected reader folder and mute notifications. Public channels can be joined directly. Private channels require the reader account to have access first.
