#!/usr/bin/env python3
"""Deliver routed Ola Radio monitor output to iMessage and OpenClaw.

``olaradio-monitor.py`` delegates routing to ``olaradio-route.py`` and embeds
that decision in each message.  This script consumes the embedded route, sends
the visible radio transcript with the current ``imsg send`` CLI, then injects
the transmission into the mapped OpenClaw session with the installed
``openclaw gateway call chat.send`` client.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


WORKSPACE = Path(
    os.environ.get("OPENCLAW_WORKSPACE", "/Users/jerclaw/.openclaw/workspace")
)
TMP_OUT = Path(
    os.environ.get("OLARADIO_MONITOR_OUTPUT", "/tmp/olaradio-monitor-output.json")
)
ROUTES_FILE = Path(
    os.environ.get("OLARADIO_ROUTES_FILE", WORKSPACE / "state/olaradio-routes.json")
)
LOG_FILE = Path(
    os.environ.get("OLARADIO_LOG_FILE", WORKSPACE / "logs/olaradio-watcher.log")
)
IMSG = os.environ.get("IMSG_BIN", "/opt/homebrew/bin/imsg")
OPENCLAW = os.environ.get("OPENCLAW_BIN", "/opt/homebrew/bin/openclaw")
JEREMY = "[REDACTED]"

# These are the production destinations requested for the two known Ola groups.
# Refuse to run if the JSON config drifts silently.
EXPECTED_ROUTES = {
    "15758": {
        "sessionKey": "agent:main:imessage:direct:[REDACTED]",
        "to": JEREMY,
    },
    "16092": {
        "sessionKey": "agent:main:imessage:group:12",
        "imsgChatId": 12,
    },
}


def log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"{message}\n")


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def validate_routes(routes: dict) -> None:
    groups = routes.get("groups")
    if not isinstance(groups, dict):
        raise ValueError("routes file must contain a 'groups' object")
    for recvid, expected in EXPECTED_ROUTES.items():
        actual = groups.get(recvid)
        if not isinstance(actual, dict):
            raise ValueError(f"required Ola Radio group {recvid} has no route")
        for key, value in expected.items():
            if actual.get(key) != value:
                raise ValueError(
                    f"group {recvid} route {key} must be {value!r}, "
                    f"not {actual.get(key)!r}"
                )


def run_checked(command: list[str], label: str, timeout: int = 15) -> bool:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"  {label} exception: {exc}")
        return False
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        log(f"  {label} failed (exit {completed.returncode}){suffix}")
        return False
    return True


def send_imessage(route: dict, text: str) -> bool:
    chat_id = route.get("imsgChatId")
    if chat_id is not None:
        command = [IMSG, "send", "--chat-id", str(chat_id), "--text", text]
    else:
        destination = str(route.get("to") or JEREMY)
        command = [IMSG, "send", "--to", destination, "--text", text]
    return run_checked(command, "imsg")


def gateway_send(session_key: str, message: str, message_id: str) -> bool:
    """Use OpenClaw's version-matched gateway client and config-based auth."""
    params = {
        "sessionKey": session_key,
        "message": message,
        "deliver": True,
        # Stable across watcher retries, so a failed batch cannot start a
        # duplicate OpenClaw run after a partial delivery.
        "idempotencyKey": f"olaradio:{message_id}" if message_id else str(uuid.uuid4()),
    }
    ok = run_checked(
        [
            OPENCLAW,
            "gateway",
            "call",
            "chat.send",
            "--params",
            json.dumps(params, separators=(",", ":")),
            "--json",
            "--timeout",
            "15000",
        ],
        "gateway",
        timeout=20,
    )
    if ok:
        log(f"  gateway: message accepted by {session_key}")
    return ok


def notify_unmatched(message: dict, text: str) -> bool:
    recvid = str(message.get("recvid") or "")
    recvnm = str(message.get("recvnm") or "")
    notification = (
        f'📻 New Ola Radio group "{recvnm}" (ID {recvid}) has no route. '
        f"Message was: {text[:100]}. Tell me where to route it."
    )
    ok = run_checked(
        [IMSG, "send", "--to", JEREMY, "--text", notification],
        "unmatched-group imsg",
    )
    if ok:
        log(f"  unmatched group {recvid} ({recvnm}), notified Jeremy")
    return ok


def main() -> int:
    try:
        routes_value = load_json(ROUTES_FILE)
        messages_value = load_json(TMP_OUT)
        if not isinstance(routes_value, dict):
            raise ValueError("routes file must contain a JSON object")
        validate_routes(routes_value)
        if not isinstance(messages_value, list):
            messages_value = [messages_value]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log(f"Delivery setup error: {exc}")
        print(f"olaradio-deliver: {exc}", file=sys.stderr)
        return 1

    groups = routes_value["groups"]
    delivered = 0
    failures = 0

    for message in messages_value:
        if not isinstance(message, dict):
            log("  invalid non-object message, skipping")
            failures += 1
            continue
        if message.get("content_type") == "system":
            continue

        recvid = str(message.get("recvid") or "")
        message_id = str(message.get("id") or "")
        recvnm = str(message.get("recvnm") or "")
        transcript = str(message.get("transcript") or "")
        sender = str(message.get("sendnm") or "Unknown")
        time_string = str(message.get("time") or "?")
        text = (
            transcript
            if transcript and transcript != "[inaudible]"
            else f"[inaudible transmission at {time_string}]"
        )

        # Prefer the router decision embedded by the monitor.  The config
        # fallback keeps old saved monitor output runnable during migration.
        route = message.get("route")
        if not isinstance(route, dict):
            configured = groups.get(recvid)
            route = {"recvid": recvid, **configured} if isinstance(configured, dict) else None

        if not route:
            if not notify_unmatched(message, text):
                failures += 1
            continue

        group_name = str(route.get("name") or recvnm or "Radio")
        imsg_text = f"📻 {sender} ({group_name}, {time_string}): {text}"
        imsg_ok = send_imessage(route, imsg_text)
        if imsg_ok:
            log(f"  imsg sent: {imsg_text[:80]}")
        else:
            failures += 1

        session_key = str(route.get("sessionKey") or "")
        gateway_ok = True
        if session_key:
            gateway_text = (
                f"📻 Ola Radio transmission from {sender} in {group_name} "
                f"(group {recvid}) at {time_string}: {text}"
            )
            gateway_ok = gateway_send(session_key, gateway_text, message_id)
            if not gateway_ok:
                failures += 1

        if imsg_ok and gateway_ok:
            delivered += 1

    log(f"Delivered {delivered} message(s); failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
