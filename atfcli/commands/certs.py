"""TLS/SSL certificate management and renewal."""

from __future__ import annotations

import time
from pathlib import Path

from ..errors import AtfError
from . import _shared as sh

NAME = "certs"
HELP = "Inspect, verify, and renew TLS/SSL certificates"


def _inspect(ctx, args) -> None:
    domain = args.domain or ctx.repo.env("ATF_DOMAIN") or ctx.repo.env("DOMAIN") or "api.legoten.com"
    cert_path = Path(f"/etc/letsencrypt/live/{domain}/fullchain.pem")
    if not cert_path.exists():
        cert_path = ctx.repo.path("certs/fullchain.pem")
    if not cert_path.exists():
        ctx.out.warn(f"No certificate found for {domain}")
        return

    res = ctx.repo.run(["openssl", "x509", "-in", str(cert_path), "-noout", "-dates", "-subject", "-issuer"], check=False)
    if not res.ok:
        ctx.out.fail(f"Failed to read certificate {cert_path}")
        return

    lines = res.stdout.splitlines()
    pairs = []
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            pairs.append((k.strip(), v.strip()))
    if args.json:
        ctx.out.json({"domain": domain, "path": str(cert_path), "details": dict(pairs)})
        return
    ctx.out.title(f"Certificate Details: {domain}")
    ctx.out.kv(pairs)


def _renew(ctx, args) -> None:
    sh.confirm_or_fail(ctx, "Renew TLS certificates via certbot?")
    ctx.out.step("Running certbot renew...")
    res = ctx.repo.run(["certbot", "renew", "--quiet"], check=False)
    if res.ok:
        ctx.out.ok("Certificates renewed successfully")
        # Reload nginx if present
        ctx.repo.run(["systemctl", "reload", "nginx"], check=False, quiet=True)
    else:
        ctx.out.warn(f"Certbot returned code {res.returncode}: {res.stderr or res.stdout}")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("inspect", aliases=["check"], help="inspect certificate validity and expiry", parents=parents)
    p.add_argument("--domain", default="", help="domain name to inspect")
    p.set_defaults(func=_inspect)

    p = sub.add_parser("renew", help="renew TLS certificates using certbot", parents=parents)
    p.set_defaults(func=_renew)
