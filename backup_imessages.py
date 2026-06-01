#!/usr/bin/env python3
"""
iMessage backup script — exports messages from ~/Library/Messages/chat.db
to Markdown and JSON files organized by contact/group chat, with optional
attachment copying.

Launchd plist example (runs daily at 2am):
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.user.imessage-backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/you/backup_imessages.py</string>
    <string>--since</string>
    <string>2024-01-01</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>2</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/tmp/imessage-backup.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/imessage-backup.err</string>
</dict>
</plist>

Install with: launchctl load ~/Library/LaunchAgents/com.user.imessage-backup.plist
"""

import argparse
import glob
import html
import json
import re
import shutil
import sqlite3
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

APPLE_EPOCH_OFFSET = 978307200  # seconds between Unix epoch and Apple epoch (2001-01-01)


def apple_time_to_datetime(ns: int) -> datetime:
    """Convert an Apple Core Data nanosecond timestamp to a UTC datetime.

    Apple's epoch starts at 2001-01-01 00:00:00 UTC, not Unix's 1970-01-01.
    chat.db stores timestamps in nanoseconds from that epoch.

    Args:
        ns: Nanoseconds since 2001-01-01 00:00:00 UTC.

    Returns:
        A timezone-aware datetime in UTC.
    """
    seconds = ns / 1e9 + APPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def safe_filename(name: str) -> str:
    """Return a filesystem-safe version of a chat or contact name.

    Replaces characters forbidden on macOS/Windows/Linux with underscores and
    strips leading/trailing dots and spaces.

    Args:
        name: Raw chat display name or phone number.

    Returns:
        A string safe to use as a directory name.
    """
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name or "Unknown"


@dataclass
class AttachmentInfo:
    name: str
    source_path: str | None
    mime: str | None
    total_bytes: int | None
    transfer_state: int | None
    dest_path: str | None = None
    status: str = "none"


@dataclass
class Message:
    rowid: int
    timestamp: datetime
    is_from_me: bool
    sender: str | None
    text: str | None
    attachments: list[AttachmentInfo] = field(default_factory=list)


@dataclass
class AttachmentStats:
    copied: int = 0
    skipped: int = 0
    missing: int = 0
    size_limit: int = 0
    bytes_copied: int = 0


def load_contacts() -> dict[str, str]:
    """Return a dict mapping normalized phone/email keys to display names.

    Phone keys are the last 10 digits of the number (handles any formatting).
    Email keys are lowercased addresses.
    Reads every AddressBook source found on this Mac.
    """
    patterns = [
        "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb",
        "~/Library/Containers/com.apple.AddressBook/Data/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb",
    ]
    contacts: dict[str, str] = {}
    for pattern in patterns:
        for db_path in glob.glob(str(Path(pattern).expanduser())):
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                for row in conn.execute("""
                    SELECT COALESCE(
                               r.ZFIRSTNAME || ' ' || r.ZLASTNAME,
                               r.ZFIRSTNAME, r.ZLASTNAME,
                               r.ZNICKNAME, r.ZORGANIZATION
                           ) AS name,
                           p.ZFULLNUMBER AS value,
                           'phone' AS kind
                    FROM ZABCDRECORD r
                    JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
                    WHERE p.ZFULLNUMBER IS NOT NULL
                    UNION ALL
                    SELECT COALESCE(
                               r.ZFIRSTNAME || ' ' || r.ZLASTNAME,
                               r.ZFIRSTNAME, r.ZLASTNAME,
                               r.ZNICKNAME, r.ZORGANIZATION
                           ) AS name,
                           e.ZADDRESSNORMALIZED AS value,
                           'email' AS kind
                    FROM ZABCDRECORD r
                    JOIN ZABCDEMAILADDRESS e ON e.ZOWNER = r.Z_PK
                    WHERE e.ZADDRESSNORMALIZED IS NOT NULL
                """):
                    name, value, kind = row["name"], row["value"], row["kind"]
                    if not name or not value:
                        continue
                    if kind == "phone":
                        digits = re.sub(r"\D", "", value)
                        key = digits[-10:] if len(digits) >= 10 else digits
                    else:
                        key = value.lower()
                    if key:
                        contacts[key] = name
                conn.close()
            except Exception:
                pass
    return contacts


def _resolve_name(handle: str, contacts: dict[str, str]) -> str:
    """Return a contact display name for a phone number or email, or handle as-is."""
    if "@" in handle:
        return contacts.get(handle.lower(), handle)
    digits = re.sub(r"\D", "", handle)
    key = digits[-10:] if len(digits) >= 10 else digits
    return contacts.get(key, handle)


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract plain text from an NSAttributedString typedstream blob.

    macOS Ventura+ stores message text in attributedBody rather than the text
    column. The blob is an NSArchiver typedstream; the UTF-8 string content is
    preceded by the marker b'\\x01+' followed by a compact-encoded length.
    """
    if not blob:
        return None
    idx = blob.find(b'\x01+')
    if idx == -1:
        return None
    pos = idx + 2
    b = blob[pos]
    if b == 0x81:
        length = int.from_bytes(blob[pos + 1:pos + 3], 'little')
        pos += 3
    elif b < 0x80:
        length = b
        pos += 1
    else:
        return None
    try:
        text = blob[pos:pos + length].decode('utf-8', errors='replace')
        return text if text.strip('￼') else None
    except Exception:
        return None


def fetch_messages(db_path: Path, since: datetime | None, contacts: dict[str, str] | None = None) -> dict[str, list[Message]]:
    """Query chat.db and return all messages grouped by chat display name.

    Opens the database read-only so it is safe to run while Messages.app is open.
    Attachment rows are joined and collected onto each Message object, but no
    files are copied here — that happens in copy_attachment().

    Args:
        db_path: Absolute path to chat.db (usually ~/Library/Messages/chat.db).
        since:   If provided, only messages on or after this UTC datetime are
                 returned.

    Returns:
        A dict mapping safe chat names to lists of Message objects ordered by
        timestamp ascending.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    since_apple = None
    if since:
        since_apple = int((since.timestamp() - APPLE_EPOCH_OFFSET) * 1e9)

    query = """
        SELECT
            m.rowid            AS msg_rowid,
            m.date             AS msg_date,
            m.is_from_me,
            m.text,
            m.attributedBody   AS attributed_body,
            COALESCE(
                (SELECT cn.display_name FROM chat cn
                 JOIN chat_message_join cmj2 ON cmj2.chat_id = cn.rowid
                 WHERE cmj2.message_id = m.rowid LIMIT 1),
                h.id
            )                  AS chat_name,
            h.id               AS handle_id,
            a.rowid            AS att_rowid,
            a.filename         AS att_filename,
            a.mime_type        AS att_mime,
            a.total_bytes      AS att_bytes,
            a.transfer_state   AS att_transfer_state
        FROM message m
        LEFT JOIN handle h ON h.rowid = m.handle_id
        LEFT JOIN message_attachment_join maj ON maj.message_id = m.rowid
        LEFT JOIN attachment a ON a.rowid = maj.attachment_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.rowid
        WHERE 1=1
    """
    params: list = []
    if since_apple is not None:
        query += " AND m.date >= ?"
        params.append(since_apple)
    query += " ORDER BY m.date ASC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    # Group rows: one message may have multiple attachment rows
    msg_map: dict[int, Message] = {}
    chat_map: dict[str, list[Message]] = defaultdict(list)

    for row in rows:
        mid = row["msg_rowid"]
        raw_name = row["chat_name"] or row["handle_id"] or "Unknown"
        if contacts and (raw_name.startswith("+") or (raw_name.startswith("(") and re.search(r"\d{7}", raw_name)) or "@" in raw_name):
            raw_name = _resolve_name(raw_name, contacts)
        chat = safe_filename(raw_name)

        if mid not in msg_map:
            ts = apple_time_to_datetime(row["msg_date"])
            raw_sender = row["handle_id"] or "Unknown"
            if contacts and raw_sender != "Unknown":
                raw_sender = _resolve_name(raw_sender, contacts)
            sender = None if row["is_from_me"] else raw_sender
            msg = Message(
                rowid=mid,
                timestamp=ts,
                is_from_me=bool(row["is_from_me"]),
                sender=sender,
                text=row["text"] or decode_attributed_body(row["attributed_body"]),
            )
            msg_map[mid] = msg
            chat_map[chat].append(msg)

        if row["att_rowid"] is not None:
            att = AttachmentInfo(
                name=Path(row["att_filename"]).name if row["att_filename"] else "unknown",
                source_path=row["att_filename"],
                mime=row["att_mime"],
                total_bytes=row["att_bytes"],
                transfer_state=row["att_transfer_state"],
            )
            msg_map[mid].attachments.append(att)

    return dict(chat_map)


def _dest_name(ts: datetime, original_name: str) -> str:
    prefix = ts.strftime("%Y-%m-%d_%H%M%S")
    safe = re.sub(r'[^\w.\-]', '_', original_name)
    return f"{prefix}_{safe}"


def copy_attachment(
    att: AttachmentInfo,
    ts: datetime,
    attachments_dir: Path,
    force: bool,
    max_bytes: int | None,
    stats: AttachmentStats,
) -> None:
    """Copy one attachment file into the chat's attachments/ directory.

    Decision logic (in order):
      1. If source_path is None → write .missing sentinel, status = "missing".
      2. If source file does not exist on disk → write .missing sentinel,
         status = "missing" (iCloud attachment never downloaded locally).
      3. If file exceeds max_bytes → skip, status = "size_limit".
      4. If dest already exists at the same size and force=False → skip,
         status = "skipped".
      5. Otherwise → copy, status = "copied".

    Mutates att.dest_path and att.status in place and updates stats.

    Args:
        att:             Attachment to process.
        ts:              Message timestamp, used as the filename prefix.
        attachments_dir: Destination directory (chat_dir/attachments/).
        force:           If True, overwrite even when sizes match.
        max_bytes:       Maximum allowed file size in bytes, or None for no limit.
        stats:           Mutable counter object updated with the outcome.
    """
    dest_name = _dest_name(ts, att.name)
    dest_path = attachments_dir / dest_name
    att.dest_path = str(Path("attachments") / dest_name)

    if att.source_path is None:
        _write_missing(dest_path, stats)
        att.status = "missing"
        return

    src = Path(att.source_path).expanduser()

    if not src.exists():
        # Check for an iCloud placeholder (.Filename.ext.icloud) and trigger download
        placeholder = src.parent / f".{src.name}.icloud"
        if placeholder.exists():
            subprocess.run(["brctl", "download", str(src)], capture_output=True)
            for _ in range(10):
                if src.exists():
                    break
                time.sleep(1)
        if not src.exists():
            _write_missing(dest_path, stats)
            att.status = "missing"
            return

    file_size = src.stat().st_size

    if max_bytes is not None and file_size > max_bytes:
        mb = file_size / (1024 * 1024)
        print(f"  [skip] {att.name} ({mb:.1f} MB) exceeds --max-attachment-mb limit")
        att.status = "size_limit"
        stats.size_limit += 1
        return

    if not force and dest_path.exists() and dest_path.stat().st_size == file_size:
        att.status = "skipped"
        stats.skipped += 1
        return

    attachments_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_path)
    att.status = "copied"
    stats.copied += 1
    stats.bytes_copied += file_size


def _write_missing(dest_path: Path, stats: AttachmentStats) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = dest_path.with_suffix(dest_path.suffix + ".missing")
    sentinel.touch()
    stats.missing += 1


_HTML_STYLE = """
  body { font-family: system-ui, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; background: #f5f5f5; color: #1a1a1a; }
  h1 { font-size: 1.1rem; color: #555; border-bottom: 1px solid #ddd; padding-bottom: .5rem; }
  .msg { margin: .6rem 0; display: flex; flex-direction: column; }
  .msg.me { align-items: flex-end; }
  .msg.them { align-items: flex-start; }
  .meta { font-size: .72rem; color: #888; margin-bottom: .2rem; }
  .bubble { max-width: 72%; padding: .5rem .8rem; border-radius: 1rem; line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
  .me .bubble { background: #0b93f6; color: #fff; border-bottom-right-radius: .25rem; }
  .them .bubble { background: #e5e5ea; color: #1a1a1a; border-bottom-left-radius: .25rem; }
  .att { font-size: .82rem; margin-top: .3rem; }
  .att a { color: inherit; }
  .att.missing { text-decoration: line-through; color: #999; }
"""


def _format_attachment_html(att: AttachmentInfo) -> str:
    name = html.escape(att.name)
    if att.dest_path and att.status in ("copied", "skipped"):
        href = quote(att.dest_path, safe='/')
        return f'<div class="att">📎 <a href="{href}">{name}</a></div>'
    elif att.status == "missing":
        return f'<div class="att missing">📎 {name} <em>(not available locally)</em></div>'
    elif att.status == "size_limit":
        return f'<div class="att">📎 {name} <em>(skipped — over size limit)</em></div>'
    return f'<div class="att">📎 {name}</div>'


def write_html(
    chat_name: str,
    messages: list[Message],
    output_dir: Path,
) -> None:
    """Write messages for one chat to per-month HTML files.

    Each file is named YYYY-MM.html and placed under output_dir/<chat_name>/.
    Attachment hrefs are relative so the file renders correctly alongside the
    attachments/ folder in any browser.
    """
    by_month: dict[str, list[Message]] = defaultdict(list)
    for msg in messages:
        key = msg.timestamp.strftime("%Y-%m")
        by_month[key].append(msg)

    chat_dir = output_dir / chat_name
    chat_dir.mkdir(parents=True, exist_ok=True)

    for month, msgs in sorted(by_month.items()):
        title = html.escape(f"{chat_name} — {month}")
        parts = [
            f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
            f'<title>{title}</title><style>{_HTML_STYLE}</style></head>',
            f'<body><h1>{title}</h1>',
        ]
        for msg in msgs:
            ts_str = html.escape(msg.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC"))
            who = "Me" if msg.is_from_me else html.escape(msg.sender or "Them")
            side = "me" if msg.is_from_me else "them"
            parts.append(f'<div class="msg {side}">')
            parts.append(f'<div class="meta">{ts_str} · {who}</div>')
            parts.append('<div class="bubble">')
            if msg.text:
                parts.append(html.escape(msg.text))
            for att in msg.attachments:
                parts.append(_format_attachment_html(att))
            parts.append('</div></div>')
        parts.append('</body></html>')

        html_path = chat_dir / f"{month}.html"
        html_path.write_text("\n".join(parts), encoding="utf-8")


def write_json(
    chat_name: str,
    messages: list[Message],
    output_dir: Path,
) -> None:
    """Write messages for one chat to per-month JSON files.

    Each file is named YYYY-MM.json and placed under output_dir/<chat_name>/.
    Each message object includes an ``attachments`` array whose entries carry
    ``attachment_status`` so downstream tools can filter by copy outcome.

    Args:
        chat_name:  Safe directory name for this chat (already sanitized).
        messages:   All messages for this chat, in any order (sorted by month).
        output_dir: Root backup directory.
    """
    by_month: dict[str, list[dict]] = defaultdict(list)
    for msg in messages:
        key = msg.timestamp.strftime("%Y-%m")
        entry: dict = {
            "rowid": msg.rowid,
            "timestamp": msg.timestamp.isoformat(),
            "is_from_me": msg.is_from_me,
            "sender": msg.sender,
            "text": msg.text,
            "attachments": [],
        }
        for att in msg.attachments:
            entry["attachments"].append(
                {
                    "has_attachment": True,
                    "attachment_name": att.name,
                    "attachment_path": att.dest_path,
                    "attachment_mime": att.mime,
                    "attachment_bytes": att.total_bytes,
                    "attachment_status": att.status,
                }
            )
        by_month[key].append(entry)

    chat_dir = output_dir / chat_name
    chat_dir.mkdir(parents=True, exist_ok=True)

    for month, entries in sorted(by_month.items()):
        json_path = chat_dir / f"{month}.json"
        json_path.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def print_summary(stats: AttachmentStats) -> None:
    """Print a one-line attachment summary and total bytes copied to stdout."""
    total = stats.copied + stats.skipped + stats.missing + stats.size_limit
    if total == 0:
        return
    gb = stats.bytes_copied / (1024 ** 3)
    mb = stats.bytes_copied / (1024 ** 2)
    size_str = f"{gb:.2f} GB" if stats.bytes_copied >= 1024 ** 3 else f"{mb:.1f} MB"
    print(
        f"\nAttachments: {stats.copied} copied, {stats.skipped} skipped (exists), "
        f"{stats.missing} missing, {stats.size_limit} over size limit"
    )
    if stats.bytes_copied:
        print(f"Total attachment size copied: {size_str}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export iMessages to Markdown/JSON.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("~/Library/Messages/chat.db").expanduser(),
        help="Path to chat.db (default: ~/Library/Messages/chat.db)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("~/iMessage-Backup").expanduser(),
        help="Output directory (default: ~/iMessage-Backup)",
    )
    parser.add_argument(
        "--since",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc),
        metavar="YYYY-MM-DD",
        help="Only export messages on or after this date",
    )
    parser.add_argument(
        "--format",
        choices=["html", "json", "both"],
        default="both",
        help="Output format — 'both' writes html + json (default: both)",
    )
    parser.add_argument(
        "--no-attachments",
        action="store_true",
        help="Skip attachment copying entirely",
    )
    parser.add_argument(
        "--force-attachments",
        action="store_true",
        help="Re-copy attachments even if destination already exists at the right size",
    )
    parser.add_argument(
        "--max-attachment-mb",
        type=float,
        metavar="MB",
        help="Skip attachments larger than this many megabytes",
    )
    args = parser.parse_args()

    max_bytes: int | None = None
    if args.max_attachment_mb is not None:
        max_bytes = int(args.max_attachment_mb * 1024 * 1024)

    contacts = load_contacts()
    print(f"Loaded {len(contacts)} contacts")
    print(f"Reading database: {args.db}")
    chats = fetch_messages(args.db, args.since, contacts)
    print(f"Found {len(chats)} chats")

    stats = AttachmentStats()

    for chat_name, messages in chats.items():
        chat_dir = args.output / chat_name
        attachments_dir = chat_dir / "attachments"

        if not args.no_attachments:
            for msg in messages:
                for att in msg.attachments:
                    copy_attachment(
                        att,
                        msg.timestamp,
                        attachments_dir,
                        force=args.force_attachments,
                        max_bytes=max_bytes,
                        stats=stats,
                    )

        if args.format in ("html", "both"):
            write_html(chat_name, messages, args.output)
        if args.format in ("json", "both"):
            write_json(chat_name, messages, args.output)

    if not args.no_attachments:
        print_summary(stats)

    print("Done.")


if __name__ == "__main__":
    main()
