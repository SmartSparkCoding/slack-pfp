"""CLI for linking and publishing self-hosted now-playing data."""
from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _post(server: str, user: str, token: str, payload: dict) -> dict:
    url = f"{server.rstrip('/')}/v1/now-playing/{user}"
    body = json.dumps(payload).encode()
    req = Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "slack-pfp-player-cli/1",
    })
    try:
        with urlopen(req, timeout=10) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        raise SystemExit(f"server returned HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc
    except URLError as exc:
        raise SystemExit(f"could not reach player bridge: {exc.reason}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Link a CLI or self-hosted player to Slack PFP")
    sub = parser.add_subparsers(dest="command", required=True)

    link = sub.add_parser("link", help="create/rotate a link token on the Slack PFP host")
    link.add_argument("slack_user_id")
    link.add_argument("--label", default="")

    revoke = sub.add_parser("revoke", help="revoke a link token")
    revoke.add_argument("slack_user_id")

    send = sub.add_parser("send", help="publish a now-playing track")
    send.add_argument("--server", required=True)
    send.add_argument("--user", required=True)
    send.add_argument("--token", required=True)
    send.add_argument("--song")
    send.add_argument("--artist")
    send.add_argument("--album", default="")
    send.add_argument("--album-art", default="")
    send.add_argument("--track-id", default="")
    send.add_argument("--ttl", type=int, default=90)
    send.add_argument("--client", default="cli")
    send.add_argument("--json", metavar="FILE", help="read payload JSON from FILE, or - for stdin")

    clear = sub.add_parser("clear", help="report that playback stopped")
    clear.add_argument("--server", required=True)
    clear.add_argument("--user", required=True)
    clear.add_argument("--token", required=True)
    clear.add_argument("--ttl", type=int, default=90)
    clear.add_argument("--client", default="cli")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"link", "revoke"}:
        import linked_player
        if args.command == "link":
            print(linked_player.create_link(args.slack_user_id, args.label))
            print("Save this token now; only its hash is stored.", file=sys.stderr)
            return 0
        print("revoked" if linked_player.revoke_link(args.slack_user_id) else "not found")
        return 0

    if args.command == "clear":
        payload = {"playing": False, "ttl": args.ttl, "client": args.client}
    elif args.json:
        stream = sys.stdin if args.json == "-" else open(args.json)
        try:
            payload = json.load(stream)
        finally:
            if stream is not sys.stdin:
                stream.close()
    else:
        if not args.song or not args.artist:
            raise SystemExit("--song and --artist are required unless --json is used")
        payload = {
            "playing": True,
            "song": args.song,
            "artist": args.artist,
            "album": args.album,
            "album_art": args.album_art,
            "track_id": args.track_id,
            "ttl": args.ttl,
            "client": args.client,
        }
    print(json.dumps(_post(args.server, args.user, args.token, payload), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
