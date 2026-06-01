# iMessage Backup

Export iMessages from `~/Library/Messages/chat.db` to HTML and JSON files,
organized by contact or group chat name, with optional attachment copying.

- Resolves phone numbers and emails to contact names via macOS Contacts
- Decodes message text from macOS Ventura+ (`attributedBody` blobs)
- Copies attachments incrementally; triggers iCloud downloads for evicted files
- No third-party dependencies — standard library only

---

## Requirements

- macOS (reads the Messages and Contacts databases, which only exist on Mac)
- Python 3.10+
- Full Disk Access granted to your terminal app (see below)

---

## Quick start

```bash
# Export everything since January 1 2025
python3 backup_imessages.py --since 2025-01-01

# Export only HTML, skip attachments
python3 backup_imessages.py --since 2025-01-01 --format html --no-attachments

# Cap attachment size and write to a custom directory
python3 backup_imessages.py --since 2025-01-01 --max-attachment-mb 25 --output ~/Backups/Messages
```

---

## Output structure

Chats are named after the contact (resolved from macOS Contacts) or the group
chat display name. Each chat gets one file per calendar month.

```
~/iMessage-Backup/
  Chris Doe/
    2025-01.html
    2025-01.json
    attachments/
      2025-01-15_143211_photo.jpg
      2025-01-22_091045_document.pdf
      2025-01-28_103000_video.mov.missing   ← iCloud attachment not on disk
  Family Group Chat/
    2025-03.html
    2025-03.json
    attachments/
      2025-03-10_180300_video.mov
```

Attachment filenames are prefixed with the **message timestamp** (`YYYY-MM-DD_HHmmss_`)
to avoid collisions. Special characters in filenames are replaced with underscores
so links work reliably in any browser.

The HTML files are self-contained — move the whole chat folder anywhere and links
to attachments still resolve.

---

## CLI reference

| Flag | Default | Description |
|------|---------|-------------|
| `--db PATH` | `~/Library/Messages/chat.db` | Path to the Messages SQLite database |
| `--output PATH` | `~/iMessage-Backup` | Root directory for exported files |
| `--since YYYY-MM-DD` | *(all messages)* | Only export messages on or after this date |
| `--format` | `both` | Output format: `html`, `json`, or `both` |
| `--no-attachments` | off | Skip attachment copying entirely |
| `--force-attachments` | off | Re-copy attachments even if destination exists at the correct size |
| `--max-attachment-mb MB` | *(no limit)* | Skip attachments larger than this many megabytes |

---

## Contact name resolution

On startup the script reads every AddressBook source in
`~/Library/Application Support/AddressBook/` and builds a lookup table of
phone numbers and email addresses → display names. Chat directory names and
sender labels in the logs both use resolved names where available.

Contacts not in your address book remain as phone numbers or email addresses.

---

## Attachment handling

Attachments are **copied, never moved** — the originals in
`~/Library/Messages/Attachments/` are never modified.

### iCloud attachments

If you have **Optimize Mac Storage** enabled, attachments may be evicted to
iCloud. The script detects the `.icloud` placeholder and triggers a download
before copying, waiting up to 10 seconds per file. Files that don't download
in time are marked missing.

To prevent eviction entirely: **System Settings → Apple ID → iCloud → iCloud Drive →
turn off Optimize Mac Storage**.

### Copy outcomes

Each attachment in the JSON output carries an `attachment_status` field:

| Status | Meaning |
| ------ | ------- |
| `copied` | File was copied to the backup |
| `skipped` | Destination already exists at the same size (use `--force-attachments` to override) |
| `missing` | File not found locally — iCloud attachment not downloaded, or since deleted |
| `size_limit` | File exceeds `--max-attachment-mb`; not copied |

A zero-byte `.missing` sentinel file is written alongside where the attachment
would have been so you can see at a glance which files weren't available.

### End-of-run summary

```
Attachments: 312 copied, 4 skipped (exists), 2 missing, 1 over size limit
Total attachment size copied: 1.2 GB
```

---

## JSON schema

Each `YYYY-MM.json` is an array of message objects:

```json
{
  "rowid": 12345,
  "timestamp": "2025-01-15T14:32:11+00:00",
  "is_from_me": false,
  "sender": "Chris Doe",
  "text": "Hey, check this out",
  "attachments": [
    {
      "has_attachment": true,
      "attachment_name": "photo.jpg",
      "attachment_path": "attachments/2025-01-15_143211_photo.jpg",
      "attachment_mime": "image/jpeg",
      "attachment_bytes": 2048576,
      "attachment_status": "copied"
    }
  ]
}
```

---

## Running automatically with launchd

Copy the plist template from the docstring at the top of `backup_imessages.py`,
save it to `~/Library/LaunchAgents/com.user.imessage-backup.plist`, then:

```bash
launchctl load ~/Library/LaunchAgents/com.user.imessage-backup.plist
```

The example plist runs the backup daily at 2 AM. Logs go to
`/tmp/imessage-backup.log` and `/tmp/imessage-backup.err`.

---

## Full Disk Access

macOS requires **Full Disk Access** for the terminal app running this script in
order to read both `~/Library/Messages/chat.db` and the AddressBook databases.

> System Settings → Privacy & Security → Full Disk Access

Add `Terminal.app` (or whichever app runs the script) to the list.
