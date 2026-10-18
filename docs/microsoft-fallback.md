# Microsoft 365 mail beside Stalwart

One MX record chooses the server for the whole domain. Two MX records do not split people. While a company moves one person at a time, Stalwart can share the domain with Exchange Online.

Set this only after the Microsoft connectors exist. The OpenComp runbook for those steps is `docs/migration/microsoft-365-coexistence.md` in the platform repository.

```yaml
mail:
  fallback:
    enabled: true
    host: contoso-com.mail.protection.outlook.com
    port: 25
```

`host` is the tenant's Exchange Online MX (`*.mail.protection.outlook.com`), not `smtp.office365.com`.

`bash apply.sh` then:

- turns on `allowRelaying` for the mail domain, so a recipient who is not a local account is accepted
- adds a relay route named `microsoft`
- delivers locally when the recipient is a Stalwart account
- relays other recipients at the same domain to `host`
- does **not** relay mail that arrived from Exchange Online's SMTP networks; that mail is delivered locally and bounces if the mailbox is not here, which stops a loop

Those networks are the Exchange Online Protection ranges published by Microsoft (worldwide endpoint id 10, recorded 2026-10-17). Replace them with `mail.fallback.source_cidrs` if a loop test shows a new range.

SPF while this is on must authorise both servers:

```text
v=spf1 mx include:spf.protection.outlook.com -all
```

`scripts/dns_records.py` prints that form when fallback is enabled, and `v=spf1 mx -all` when it is not.

## Who has a mailbox

Kanidm group `mail-users` is the switch.

- A person not in the group can use files and chat. They have no Stalwart mailbox, and webmail sign-in is refused.
- Joining the group creates the mailbox on the next apply, and again within about five minutes. They do not have to sign in first.
- Leaving the group deletes a mailbox this kit created for them. Copy the mail out before that. A shared mailbox that was never tied to the group is left alone.

Turn fallback off by removing the `mail.fallback` block, or setting `enabled: false`, and applying again. That stops relaying and removes the `microsoft` route.
