# Stalwart Easy Deploy

Opinionated, wizard-driven [Stalwart](https://stalw.art/) mail server plus [Bulwark](https://bulwarkmail.org/) webmail for a single VPS: Docker Compose, `deploy.yaml` configuration, and generated secrets.

Caddy terminates HTTPS for the admin UI, JMAP, and webmail. Mail protocols (SMTP/IMAP/Sieve) bind directly on the host.

## Requirements

- Linux host with Docker Engine and Docker Compose v2
- DNS `A`/`AAAA` for the mail hostname (e.g. `mail.example.com`) and webmail domain
- Ports **25, 465, 587, 993, 4190** open in addition to 80/443
- [uv](https://docs.astral.sh/uv/) (installed automatically by `ensure-dependencies.sh`)

### Proxy modes

- **`proxy.mode: standalone`** (default) — this repo runs `stalwart_caddy` on ports 80/443.
- **`proxy.mode: integrate`** — no local Caddy; emits a fragment for [easydeploy-engine](../easydeploy-engine/) (multi-service VPS). See [docs/integrating-engine.md](docs/integrating-engine.md).

## Quick start

```bash
git clone --recurse-submodules https://github.com/opencomp-eu/stalwart-easy-deploy.git
cd stalwart-easy-deploy
bash ensure-dependencies.sh
bash wizard.sh
```

Or manually:

```bash
cp deploy.yaml.example deploy.yaml
# edit deploy.yaml — set hostname, domain, and webmail domain
bash apply.sh
```

After the stack is up:

1. Open `https://mail.example.com/admin` and sign in with the **Stalwart recovery admin** from `.stalwart-easy-deploy/secrets.yaml` (`RECOVERY_ADMIN_PASSWORD`). This is not the Authelia user.
2. Complete the Stalwart setup wizard (hostname + domain). **Disable ACME HTTP-01** — Caddy already terminates HTTPS on 443.
3. Choose console logging (Docker captures stdout).
4. Restart Stalwart when the wizard asks: `docker restart stalwart`.
5. Re-run `bash apply.sh` (and `easydeploy-engine` `apply.sh --skip-kits` in integrate mode) so Caddy keeps proxying to Stalwart HTTP `:8080`.
6. Publish MX / SPF / DKIM / DMARC from the Stalwart WebUI (Management → Domains → DNS zone file).
7. Sign in to Bulwark at `https://webmail.example.com` with a mailbox created in Stalwart.

**Bulwark must use a different hostname** than Stalwart (`webmail.example.com` vs `mail.example.com`). Both apps serve `/api` and OAuth on the same paths, so they cannot share a host.

## Configuration

- **`deploy.yaml`** — operator settings (hostname, domains, image tags, ports).
- **`.stalwart-easy-deploy/secrets.yaml`** — generated recovery password and Bulwark session secret (do not commit).
- **`/var/lib/stalwart`** (default) — Stalwart config (`etc/`) and mail data (`data/`).
- **`/var/lib/bulwark`** (default) — Bulwark settings, admin config, and telemetry.

Pin image tags in `deploy.yaml` (`stalwart.tag`, `bulwark.tag`) instead of floating `latest` for production.

Set `bulwark.enabled: false` to run Stalwart without webmail.

## TLS for IMAP/SMTP

Caddy owns port 443, so Stalwart cannot complete HTTP-01 ACME for mail ports. After first boot, either:

- Configure **DNS-01 ACME** in the Stalwart WebUI for `mail.example.com`, or
- Copy the Caddy-issued certificate into Stalwart (see [Stalwart’s Caddy guide](https://stalw.art/docs/server/reverse-proxy/caddy/)).

Until then, IMAPS/SMTPS present Stalwart’s fallback certificate.

## Troubleshooting

### `https://mail…/admin` returns 502

Caddy is up but cannot reach the Stalwart container. Webmail may still load; JMAP then looks like a CORS error because OPTIONS is answered by Caddy while GET/POST to `mail…` is 502.

First check whether Stalwart is running:

```bash
docker ps -a --filter name=stalwart
docker logs stalwart --tail 80
docker exec easydeploy_caddy wget -S --timeout=5 http://stalwart:8080/admin -O /dev/null
docker exec stalwart bash -c 'echo >/dev/tcp/127.0.0.1/8080 && echo 8080-ok'
```

Do **not** probe both `stalwart:8080` and `stalwart:443` from Caddy. Stalwart treats
that as a port scan and bans Caddy's IP.

If the container exited or is restarting, `docker restart stalwart` often brings it back. If `config.json` is missing under `{data_dir}/etc`, the wizard did not persist and Stalwart is in bootstrap again (`chown 2000:2000` that directory).

If `127.0.0.1:8080` inside Stalwart returns HTTP 302 while Caddy receives
`Connection reset by peer` (or `/admin` is 403 then goes silent), Stalwart has
**scan-banned the Caddy container IP** on `easydeploy-net` (often `172.19.0.x`).
That is not the Docker healthcheck (healthchecks come from `127.0.0.1`). Caddy
is banned when Stalwart sees exploit URL probes or connections to both HTTP
`:8080` and HTTPS `:443` from the proxy IP.

Unban Docker/Caddy and allowlist the Docker bridge pool (uses `curl` already
in the Stalwart container — no extra service):

```bash
cd /root/stalwart-easy-deploy
bash apply.sh --unlock-proxy
```

Then re-apply so Caddy drops scanner paths and talks to a single upstream:

```bash
bash apply.sh
# integrate mode: also refresh shared Caddy
cd /root/easydeploy-engine && bash apply.sh --skip-kits
```

`apply.sh` also sets `Http.useXForwarded=true` so later scan-bans apply to the
real client, not Caddy. `SystemSettings.proxyTrustedNetworks` must stay empty
(this kit does not use Proxy Protocol). Caddy proxies plain HTTP to
`stalwart:8080`.

Mail ports are published directly by Docker, so this deployment does not need
Proxy Protocol on any listener.

Confirm the live Caddyfile uses `reverse_proxy stalwart:8080` first:

```bash
sed -n '/^mail.opencomp.eu {/,/^}/p' /root/easydeploy-engine/caddy/Caddyfile
```

### Bulwark CORS errors against `mail…`

Stalwart’s “permissive CORS” sends `Access-Control-Allow-Origin: *` and does not send `Allow-Credentials`. Bulwark’s browser JMAP calls are credentialed, so the browser still blocks them.

Caddy on the mail host must:

- answer OPTIONS itself (204)
- replace Stalwart’s `*` with `https://<webmail-host>`
- set `Access-Control-Allow-Credentials: true`
- never send `Access-Control-Expose-Headers: *` together with credentials

After changing Caddy, re-apply this kit and the engine (`apply.sh --skip-kits`). Permissive CORS in the Stalwart WebUI can stay on or off; Caddy overrides the origin header.

Check with:

```bash
curl -sSI -H 'Origin: https://webmail.example.com' https://mail.example.com/.well-known/jmap
```

The 307 to `/jmap/session` must include `access-control-allow-origin: https://webmail.example.com` (not `*`). `curl -I` (HEAD) returns 404 here; use GET as above.

### Bulwark: “Write permission denied on settings data directory”

The image runs as UID **1001** (`nextjs`). `apply.sh` chowns `bulwark.data_dir` to that user. If you created the directory by hand as root, fix it once:

```bash
chown -R 1001:1001 /var/lib/bulwark
docker restart bulwark
```

### Sending mail does nothing

Finish the Stalwart setup wizard at `https://<mail-host>/admin` (disable HTTP-01 ACME; Caddy already terminates HTTPS). Until the default domain exists, JMAP login can work while outbound submission does not. Then publish MX/SPF/DKIM/DMARC from Management → Domains.

## Day-to-day

```bash
bash apply.sh              # re-render config and reconcile stack
bash apply.sh --skip-runtime   # render only, no docker
bash start.sh              # compose up (via apply, skip pull)
bash stop.sh               # compose down
```

## Backups

Back up:

- `stalwart.data_dir` (config + mail store)
- `bulwark.data_dir` (webmail settings)
- `.stalwart-easy-deploy/secrets.yaml`

## Development

```bash
uv sync --dev
uv run pytest
```

## License

Same as sibling easy-deploy projects (add license file if publishing).
