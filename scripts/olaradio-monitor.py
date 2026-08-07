#!/usr/bin/env python3
"""Detect, transcribe, and route new Ola Radio transmissions.

The monthly ``poc_chat_record_YYYYMM`` tables are read from the official
application's local SQLite database.  Routing is delegated to
``olaradio-route.py`` before the resulting JSON is emitted.

Dry-run mode is deliberately side-effect free: it reads the local database and
route configuration, but does not transcribe audio, update checkpoints, write
temporary files, send messages, or connect to the OpenClaw gateway.

Usage:
  python3 olaradio-monitor.py [--seed] [--since TIMESTAMP] [--dry-run]
  python3 olaradio-monitor.py --commit-output monitor-output.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


WORKSPACE = Path(
    os.environ.get("OPENCLAW_WORKSPACE", "/Users/jerclaw/.openclaw/workspace")
)
CONTAINER = Path(
    os.environ.get(
        "OLARADIO_CONTAINER",
        "/Users/jerclaw/Library/Containers/com.aewt.app.friends/Data",
    )
)
DB = Path(os.environ.get("OLARADIO_DB", CONTAINER / "Documents/poc_chat_record.db"))
STATE_DIR = Path(os.environ.get("OLARADIO_STATE_DIR", WORKSPACE / "state"))
STATE_FILE = STATE_DIR / "olaradio-last-ts"
SEEN_FILE = STATE_DIR / "olaradio-seen-ids.json"
ROUTE_SCRIPT = Path(
    os.environ.get("OLARADIO_ROUTE_SCRIPT", WORKSPACE / "scripts/olaradio-route.py")
)
ROUTES_FILE = Path(
    os.environ.get("OLARADIO_ROUTES_FILE", STATE_DIR / "olaradio-routes.json")
)
TMP_DIR = Path(os.environ.get("OLARADIO_TMP_DIR", "/tmp/olaradio-processing"))

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
TABLE_RE = re.compile(r"^poc_chat_record_\d{6}$")
MAX_SEEN = 2000


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        action="store_true",
        help="set the checkpoint to the newest local message without processing it",
    )
    parser.add_argument(
        "--since",
        type=int,
        metavar="TIMESTAMP",
        help="scan from this millisecond timestamp instead of the checkpoint",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview transcription/routing without writing state or delivering",
    )
    parser.add_argument(
        "--defer-state",
        action="store_true",
        help="emit messages without committing state (the watcher uses this)",
    )
    parser.add_argument(
        "--commit-output",
        type=Path,
        metavar="JSON_FILE",
        help="commit IDs/timestamps from successfully delivered monitor output",
    )
    args = parser.parse_args(argv)
    if args.seed and (args.dry_run or args.defer_state or args.commit_output):
        parser.error("--seed cannot be combined with dry/deferred/commit modes")
    if args.dry_run and (args.defer_state or args.commit_output):
        parser.error("--dry-run cannot be combined with deferred/commit modes")
    if args.commit_output and args.since is not None:
        parser.error("--commit-output cannot be combined with --since")
    return args


def atomic_write_text(path: Path, value: str) -> None:
    """Replace a small state file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def load_seen_ids() -> tuple[list[str], set[str]]:
    """Load IDs in insertion order while accepting the old JSON-list format."""
    if not SEEN_FILE.exists():
        return [], set()
    try:
        value = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("seen-ID file must contain a JSON list")
        ordered = list(dict.fromkeys(str(item) for item in value if item))
        return ordered[-MAX_SEEN:], set(ordered[-MAX_SEEN:])
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"warning: ignoring invalid seen-ID file: {exc}", file=sys.stderr)
        return [], set()


def save_seen_ids(existing: Iterable[str], new_ids: Iterable[str]) -> None:
    ordered = list(dict.fromkeys([*existing, *new_ids]))[-MAX_SEEN:]
    atomic_write_text(SEEN_FILE, json.dumps(ordered, ensure_ascii=False) + "\n")


def load_checkpoint() -> int | None:
    if not STATE_FILE.exists():
        return None
    try:
        return int(STATE_FILE.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid checkpoint {STATE_FILE}: {exc}") from exc


def get_available_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name LIKE 'poc_chat_record_%' "
        "ORDER BY name"
    ).fetchall()
    tables = [str(row[0]) for row in rows if TABLE_RE.fullmatch(str(row[0]))]
    return tables


def quoted_identifier(name: str) -> str:
    if not TABLE_RE.fullmatch(name):
        raise ValueError(f"unsafe Ola Radio table name: {name!r}")
    return f'"{name}"'


def max_timestamp(conn: sqlite3.Connection, tables: Iterable[str]) -> int:
    newest = 0
    for table in tables:
        value = conn.execute(
            f"SELECT COALESCE(MAX(timestamp), 0) FROM {quoted_identifier(table)}"
        ).fetchone()[0]
        newest = max(newest, int(value or 0))
    return newest


def transcribe_audio(msg_id: str, max_retries: int = 15) -> tuple[str | None, str | None]:
    """Extract a locally cached audio blob and transcribe it with Whisper."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = TMP_DIR / f"{msg_id}.mp3"

    for _attempt in range(max_retries):
        # Do not use immutable=1 here: the blob may have just arrived via WAL.
        with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as blob_conn:
            row = blob_conn.execute(
                "SELECT byteData FROM chat_blob_data WHERE id = ?", (msg_id,)
            ).fetchone()
        if row and row[0]:
            audio_path.write_bytes(row[0])
            break
        time.sleep(1)
    else:
        return None, None

    output_dir = TMP_DIR / msg_id
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = TMP_DIR / f"{msg_id}_norm.wav"

    try:
        ffmpeg = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(audio_path),
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(normalized_path),
            ],
            capture_output=True,
            timeout=30,
            text=True,
        )
        whisper_input = normalized_path if ffmpeg.returncode == 0 and normalized_path.exists() else audio_path
    except (OSError, subprocess.SubprocessError):
        whisper_input = audio_path

    try:
        whisper = subprocess.run(
            [
                "whisper",
                str(whisper_input),
                "--model",
                "base",
                "--language",
                "en",
                "--output_format",
                "txt",
                "--output_dir",
                str(output_dir),
            ],
            capture_output=True,
            timeout=60,
            text=True,
        )
        if whisper.returncode != 0:
            detail = whisper.stderr.strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            return f"[transcription failed{suffix}]", str(audio_path)

        txt_file = output_dir / f"{whisper_input.stem}.txt"
        if not txt_file.exists():
            candidates = sorted(output_dir.glob("*.txt"))
            txt_file = candidates[0] if candidates else txt_file
        if not txt_file.exists():
            return "[transcription failed]", str(audio_path)

        lines = [
            line
            for line in txt_file.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("[")
        ]
        return " ".join(lines).strip(), str(audio_path)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"[transcription error: {exc}]", str(audio_path)


def classify_message(message: dict, dry_run: bool) -> dict:
    msgtype = str(message.get("msgtype", ""))
    msgdata = message.get("msgdata") or ""
    timestamp = int(message.get("timestamp") or 0)

    if timestamp > 1_000_000_000_000:
        message["time"] = datetime.fromtimestamp(
            timestamp / 1000, tz=LOCAL_TZ
        ).strftime("%I:%M %p")

    if msgtype == "4":
        message["content_type"] = "audio"
        if dry_run:
            message["transcript"] = "[dry-run: audio transcription skipped]"
            message["audio_path"] = None
            message["dry_run_action"] = "would transcribe cached audio, route, and deliver"
        else:
            transcript, audio_path = transcribe_audio(str(message["id"]))
            message["transcript"] = transcript or "[inaudible]"
            message["audio_path"] = audio_path
    elif msgtype == "100":
        message["content_type"] = "system"
        message["transcript"] = msgdata
        if dry_run:
            message["dry_run_action"] = "would skip system message"
    else:
        message["content_type"] = "text"
        message["transcript"] = msgdata
        if dry_run:
            message["dry_run_action"] = "would route and deliver text"
    return message


def route_messages(messages: list[dict]) -> list[dict]:
    """Call the dedicated router and return its enriched message list."""
    completed = subprocess.run(
        [
            sys.executable,
            str(ROUTE_SCRIPT),
            "--stdin",
            "--routes",
            str(ROUTES_FILE),
        ],
        input=json.dumps(messages, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "unknown router failure"
        raise RuntimeError(f"olaradio-route.py failed: {detail}")
    try:
        routed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"olaradio-route.py returned invalid JSON: {exc}") from exc
    if not isinstance(routed, list):
        raise RuntimeError("olaradio-route.py did not return a JSON list")
    return routed


def scan_messages(
    conn: sqlite3.Connection,
    tables: Iterable[str],
    since: int,
    seen_ids: set[str],
    dry_run: bool,
) -> tuple[list[dict], int, list[str]]:
    messages: list[dict] = []
    new_ids: list[str] = []
    newest = since

    # Include the checkpoint itself so a second row with the same millisecond
    # timestamp cannot be lost.  The seen-ID set suppresses the already handled
    # row at that boundary.
    comparator = ">=" if since else ">"
    for table in tables:
        cursor = conn.execute(
            "SELECT id, sendid, sendnm, recvid, recvnm, recvtype, "
            "msgtype, msgdata, timestamp, readStatus "
            f"FROM {quoted_identifier(table)} "
            f"WHERE timestamp {comparator} ? ORDER BY timestamp ASC, id ASC",
            (since,),
        )
        for row in cursor:
            message = dict(row)
            msg_id = str(message.get("id") or "")
            timestamp = int(message.get("timestamp") or 0)
            newest = max(newest, timestamp)
            if not msg_id or msg_id in seen_ids:
                continue
            messages.append(classify_message(message, dry_run))
            new_ids.append(msg_id)
            seen_ids.add(msg_id)

    messages.sort(key=lambda item: (int(item.get("timestamp") or 0), str(item.get("id") or "")))
    return messages, newest, new_ids


def commit_output(path: Path) -> None:
    """Commit a monitor batch only after the watcher delivered it successfully."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read delivered monitor output {path}: {exc}") from exc
    if not isinstance(value, list):
        value = [value]

    ids: list[str] = []
    newest = load_checkpoint() or 0
    for message in value:
        if not isinstance(message, dict):
            raise RuntimeError("delivered monitor output contains a non-object")
        msg_id = str(message.get("id") or "")
        if msg_id:
            ids.append(msg_id)
        try:
            newest = max(newest, int(message.get("timestamp") or 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid message timestamp for {msg_id or '?'}") from exc

    seen_order, _seen_ids = load_seen_ids()
    save_seen_ids(seen_order, ids)
    atomic_write_text(STATE_FILE, f"{newest}\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.commit_output:
        try:
            commit_output(args.commit_output)
            return 0
        except (OSError, RuntimeError) as exc:
            print(f"olaradio-monitor: {exc}", file=sys.stderr)
            return 1

    if not DB.exists():
        print("[]")
        return 0

    try:
        with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            tables = get_available_tables(conn)

            if args.seed:
                atomic_write_text(STATE_FILE, f"{max_timestamp(conn, tables)}\n")
                print("[]")
                return 0

            checkpoint = args.since if args.since is not None else load_checkpoint()
            if checkpoint is None:
                # Preserve the established safe first-run behavior: do not replay
                # all historical radio traffic merely because state is absent.
                if args.dry_run:
                    print("[]")
                    return 0
                else:
                    atomic_write_text(STATE_FILE, f"{max_timestamp(conn, tables)}\n")
                    print("[]")
                    return 0

            seen_order, seen_ids = load_seen_ids()
            messages, newest, new_ids = scan_messages(
                conn, tables, checkpoint, seen_ids, args.dry_run
            )

        routed = route_messages(messages)

        if not args.dry_run and not args.defer_state:
            # Commit only after every table was read and routing succeeded.
            save_seen_ids(seen_order, new_ids)
            atomic_write_text(STATE_FILE, f"{newest}\n")

        print(json.dumps(routed, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"olaradio-monitor: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
