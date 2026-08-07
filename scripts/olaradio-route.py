#!/usr/bin/env python3
"""Attach configured Ola Radio routing decisions to monitor messages.

The router is intentionally side-effect free.  It reads a JSON message list
from stdin (or ``--input``), adds ``route_status`` and ``route`` fields, and
writes the enriched list to stdout.  Delivery consumes this exact decision, so
the monitor and delivery stages cannot disagree about a group destination.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


DEFAULT_INPUT = Path("/tmp/olaradio-monitor-output.json")
DEFAULT_ROUTES = Path(
    "/Users/jerclaw/.openclaw/workspace/state/olaradio-routes.json"
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--stdin", action="store_true", help="read messages from stdin")
    source.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"message JSON file (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--routes",
        type=Path,
        default=DEFAULT_ROUTES,
        help=f"route JSON file (default: {DEFAULT_ROUTES})",
    )
    return parser.parse_args(argv)


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def route_messages(messages: list[dict], routes: dict) -> list[dict]:
    groups = routes.get("groups")
    if not isinstance(groups, dict):
        raise ValueError("routes file must contain a 'groups' object")

    routed: list[dict] = []
    for original in messages:
        if not isinstance(original, dict):
            raise ValueError("every monitor message must be a JSON object")
        message = dict(original)

        if message.get("content_type") == "system":
            message["route_status"] = "skipped-system"
            message["route"] = None
        else:
            recvid = str(message.get("recvid") or "")
            configured = groups.get(recvid)
            if isinstance(configured, dict):
                # Copy the route so later stages receive a stable decision while
                # retaining recvid and every source/routing field on the message.
                message["route_status"] = "routed"
                message["route"] = {"recvid": recvid, **configured}
            else:
                message["route_status"] = "unmatched"
                message["route"] = None
        routed.append(message)
    return routed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.stdin:
            messages_value = json.load(sys.stdin)
        else:
            messages_value = load_json(args.input)
        routes_value = load_json(args.routes)

        if not isinstance(messages_value, list):
            messages_value = [messages_value]
        if not isinstance(routes_value, dict):
            raise ValueError("routes file must contain a JSON object")

        print(
            json.dumps(
                route_messages(messages_value, routes_value),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"olaradio-route: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
