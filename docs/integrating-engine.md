# Integrating with Easy Deploy Engine

For a **multi-service VPS**, use `proxy.mode: integrate` so this kit does not bind :443.

```yaml
proxy:
  type: caddy
  mode: integrate
  integrate:
    network: easydeploy-net
```

Then run `bash wizard.sh` in [easydeploy-engine](../easydeploy-engine/) (it can clone this repo as a sibling if needed), or apply this kit, then the engine, by hand. The engine wizard sets `proxy.mode: integrate` and starts shared Caddy.

Manual equivalent:

1. `bash apply.sh` here — writes `.stalwart-easy-deploy/integration/caddy.caddy`, stops `stalwart_caddy`, and joins `easydeploy-net`
2. `bash apply.sh --skip-kits` in easydeploy-engine so shared Caddy reloads the fragment

After the Stalwart setup wizard, run both steps again. Caddy keeps proxying to `stalwart:8080`; it does not switch to container HTTPS `:443`.

Mail ports (25, 465, 587, 993, 4190) stay bound on the host in both modes — Caddy only terminates HTTPS for the admin UI, JMAP, and Bulwark.

Each kit uses a distinct Compose project name (`stalwart-easy-deploy`, `easydeploy-engine`) so one kit's `docker compose up --remove-orphans` does not remove the other's containers.

Standalone mode (`mode: standalone`, default) keeps the local `stalwart_caddy` container.

## Kanidm identity

On a same-VPS engine install, easydeploy-engine writes `.stalwart-easy-deploy/integration/identity-provider.yaml`. After bootstrap, apply upserts a Kanidm LDAP directory (`ldaps://kanidm:3636`) by URL (and removes leftover duplicates from earlier applies) and **always selects LDAP** as the authentication directory so **Bulwark / JMAP / IMAP password login** works with the Kanidm username (`thomas@opencomp.eu`) and Kanidm password. Stalwart’s OIDC directory cannot accept a password (`Unsupported credentials type for OIDC backend`); `identity.auth_directory: oidc` is ignored for that reason. The old local Stalwart mailbox password no longer applies once LDAP is selected. Passkeys used on the Kanidm portal do not bind over LDAP. An OIDC client (`stalwart-webui`) is still registered for later browser SSO; it is not the selected directory.

Set `identity.managed: false` to keep Stalwart's internal directory.

See [easydeploy-engine/docs/integrated-vps.md](../easydeploy-engine/docs/integrated-vps.md).
