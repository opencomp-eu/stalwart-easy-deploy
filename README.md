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
5. Publish MX / SPF / DKIM / DMARC from the Stalwart WebUI (Management → Domains → DNS zone file).
6. Sign in to Bulwark at `https://webmail.example.com` with a mailbox created in Stalwart.

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
