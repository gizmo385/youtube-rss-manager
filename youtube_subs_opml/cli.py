from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .oauth import get_credentials, run_auth_flow
from .opml import build_opml
from .youtube import fetch_subscriptions


def default_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "youtube-subs-opml"


def cmd_auth(args: argparse.Namespace) -> int:
    client_secrets = Path(args.client_secrets).expanduser()
    if not client_secrets.exists():
        print(
            f"client_secrets file not found: {client_secrets}\n"
            "Download it from Google Cloud Console (APIs & Services > Credentials).",
            file=sys.stderr,
        )
        return 1
    token = Path(args.token).expanduser()
    run_auth_flow(client_secrets, token)
    print(f"Saved credentials to {token}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    token = Path(args.token).expanduser()
    creds = get_credentials(token)
    subs = fetch_subscriptions(creds)
    opml = build_opml(subs, title=args.title)
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(opml)
        print(f"Wrote {len(subs)} subscriptions to {output}", file=sys.stderr)
    else:
        sys.stdout.write(opml)
    return 0


def main(argv: list[str] | None = None) -> int:
    config_dir = default_config_dir()
    parser = argparse.ArgumentParser(prog="youtube-subs-opml")
    parser.add_argument(
        "--client-secrets",
        default=str(config_dir / "client_secrets.json"),
        help="Path to OAuth client secrets JSON (default: %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=str(config_dir / "token.json"),
        help="Path to stored credentials (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_auth = sub.add_parser("auth", help="Run the OAuth flow and store a refresh token")
    p_auth.set_defaults(func=cmd_auth)

    p_gen = sub.add_parser("generate", help="Fetch subscriptions and emit OPML")
    p_gen.add_argument("-o", "--output", help="Output OPML path (default: stdout)")
    p_gen.add_argument("--title", default="YouTube Subscriptions")
    p_gen.set_defaults(func=cmd_generate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
