#!/usr/bin/env python3
"""Choose a task-compatible JC Dream subagent model without fixed rotation."""

from __future__ import annotations

import argparse
import json
import secrets
import sys


GENERAL_POOL = (
    "zai/glm-5.2",
    "qwen/qwen3.8-max-preview",
    "openai/gpt-5.6-sol",
)
CREATIVE_POOL = ("zai/glm-5.2",)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--category",
        choices=("general", "research", "coding", "sysadmin", "creative"),
        default="general",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Model unavailable or unsuitable for this run; repeat as needed.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pool = CREATIVE_POOL if args.category == "creative" else GENERAL_POOL
    eligible = tuple(model for model in pool if model not in set(args.exclude))
    if not eligible:
        print("no eligible models remain", file=sys.stderr)
        return 2

    selected = secrets.choice(eligible)
    if args.json:
        print(
            json.dumps(
                {
                    "category": args.category,
                    "model": selected,
                    "eligible_pool": eligible,
                },
                separators=(",", ":"),
            )
        )
    else:
        print(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
