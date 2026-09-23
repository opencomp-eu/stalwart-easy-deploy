"""Print the DNS records a Stalwart workspace needs, as JSON.

DKIM is included only when --dkim-txt is passed. Stalwart creates that key
during apply, so a fresh config has a pending DKIM record. Address records
use null values until the platform knows the VPS addresses.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


def dns_records(
    config: dict,
    *,
    dkim_txt: str | None = None,
    dkim_selector: str = "default",
) -> dict:
    stalwart = config.get("stalwart") or {}
    bulwark = config.get("bulwark") or {}
    hostname = str(stalwart.get("hostname") or "").strip().rstrip(".")
    domain = str(stalwart.get("domain") or "").strip().rstrip(".")
    if not hostname or not domain:
        raise ValueError("stalwart.hostname and stalwart.domain are required")

    webmail = ""
    if bulwark.get("enabled", True):
        webmail = str(bulwark.get("domain") or "").strip().rstrip(".")

    records: list[dict] = [
        _address("mail-a", "A", hostname, "server_ipv4"),
        _address("mail-aaaa", "AAAA", hostname, "server_ipv6"),
        {
            "purpose": "mx",
            "type": "MX",
            "name": domain,
            "priority": 10,
            "value": hostname,
        },
        {
            "purpose": "spf",
            "type": "TXT",
            "name": domain,
            "value": "v=spf1 mx -all",
        },
        {
            "purpose": "dmarc",
            "type": "TXT",
            "name": f"_dmarc.{domain}",
            "value": f"v=DMARC1; p=quarantine; rua=mailto:dmarc@{domain}",
        },
        {
            "purpose": "dkim",
            "type": "TXT",
            "name": f"{dkim_selector}._domainkey.{domain}",
            "value": dkim_txt,
            "status": "ready" if dkim_txt else "pending",
        },
        {
            "purpose": "ptr",
            "type": "PTR",
            "name": None,
            "value": hostname,
            "comment": "Set reverse DNS at the hosting provider for the VPS address.",
        },
    ]
    if webmail:
        records[2:2] = [
            _address("webmail-a", "A", webmail, "server_ipv4"),
            _address("webmail-aaaa", "AAAA", webmail, "server_ipv6"),
        ]

    return {
        "version": 1,
        "domain": domain,
        "mail_hostname": hostname,
        "webmail_hostname": webmail or None,
        "records": records,
    }


def _address(purpose: str, record_type: str, name: str, fill_from: str) -> dict:
    return {
        "purpose": purpose,
        "type": record_type,
        "name": name,
        "value": None,
        "fill_from": fill_from,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print required Stalwart DNS records as JSON.")
    parser.add_argument("--config", default="deploy.yaml", help="Path to deploy.yaml")
    parser.add_argument("--dkim-txt", default=None, help="DKIM TXT value from Stalwart, if it already exists")
    parser.add_argument("--dkim-selector", default="default")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)

    path = Path(args.config)
    config = yaml.safe_load(path.read_text()) or {}
    payload = dns_records(config, dkim_txt=args.dkim_txt, dkim_selector=args.dkim_selector)
    json.dump(payload, sys.stdout, indent=None if args.compact else 2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
