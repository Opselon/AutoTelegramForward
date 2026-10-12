# Web Dashboard URL & TLS

The bot's dashboard panel shows a **public URL** and a one-tap login button. The
link is opened on the user's own device, so `localhost` never works — the URL
must point at a reachable address of this server.

## Where the URL comes from (precedence)

1. **Bot-side setting** (highest) — set from the bot's main menu:
   **🔗 Dashboard URL → ✏️ Set Dashboard URL**. Stored in the database, so it
   survives a restart. Clear it with 🗑 Reset to auto.
2. **`ATF_WEB_URL`** env var / `web_url:` in `config.yaml`.
3. **Auto-detected host address** (lowest) — first global IP on the host's
   interfaces, port `8088`. Only useful in source mode; a Docker container can
   only see its private bridge IP, so set the URL explicitly there.

The URL must include the scheme, and the port if non-default:
`https://panel.example.com` or `http://203.0.113.10:8088`.

## Docker

Add to `.env`:

```env
ATF_WEB_URL=http://203.0.113.10:8088
```

Or skip it and set the URL from the bot (recommended — no restart needed).

## TLS with Caddy (optional, automatic certificates)

A Caddy reverse proxy is included but disabled by default. It provisions a
Let's Encrypt certificate automatically for your domain and terminates TLS for
the dashboard.

1. Point your domain's A/AAAA record at this server.
2. In `.env`, set the domain and a contact email for the ACME account:

   ```env
   ATF_TLS_DOMAIN=panel.example.com
   ATF_TLS_EMAIL=admin@example.com
   ATF_WEB_URL=https://panel.example.com
   ```

3. Start the proxy alongside the other services:

   ```bash
   docker compose --profile tls up -d
   ```

Caddy binds `:80` and `:443`. HTTP is proxied and, once a domain is configured,
redirected to HTTPS. Certificates are stored in the `caddy-data` volume and
renewed automatically.

If your host blocks port `80` or `443`, open them in its firewall — the ACME
HTTP-01 challenge needs port `80` to prove domain ownership.

## Source mode (no Docker)

Run Caddy on the host in front of the gateway:

```bash
# /etc/caddy/Caddyfile
panel.example.com {
    reverse_proxy 127.0.0.1:8088
}
```

```bash
sudo systemctl enable --now caddy
```

Then set the bot's dashboard URL to `https://panel.example.com`.
