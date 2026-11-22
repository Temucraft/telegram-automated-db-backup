#!/usr/bin/env python3
"""Temucraft nightly backup: every realm database, the day's MySQL binary logs and system journal, each in an
encrypted 7z, uploaded to a Telegram chat by a bot. Local copies stay 30 days. Binary logs are purged and the journal
trimmed only when every upload worked; any failure pings in Discord's logs channel.

Settings: /root/AzerothCore/telegram-backup.env (TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID, ARCHIVE_PASSWORD). Extract: 7z x <file>.7z.001 (asks for the password)."""
import asyncio
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

asyncio.set_event_loop(asyncio.new_event_loop())  # pyrogram needs a loop at import time
from pyrogram import Client  # noqa: E402
from pyrogram.errors import FloodWait, RPCError  # noqa: E402
import pyrogram.utils  # noqa: E402

pyrogram.utils.MIN_CHANNEL_ID = -1009999999999  # pyrogram 2.0.106 rejects the newer, longer supergroup ids

ENV = "/root/AzerothCore/telegram-backup.env"
BACKUPS = "/root/AzerothCore/Backups"
DATABASES = ["acore_auth", "acore_characters", "acore_world", "acore_playerbots",
             "acore_ptr_characters", "acore_ptr_world", "acore_ptr_playerbots", "acore_aowow"]
KEEP_DAYS = 30            # local copies
TZ = ZoneInfo("Europe/Lisbon")   # names like the Minecraft backups: DD_MM_YYYY_HH-MM-SS_<name>
JOURNAL_KEEP = "3d"       # journal kept on disk after a good run
VOLUME = "1400m"          # Telegram takes files up to 2 GB
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")  # last good journal export
DISCORD_ENV = "/root/Manage/Discord-Bot/.env"
DISCORD_LOGS = "1570664060092153856"
DISCORD_USER = "151411715769106432"


def load_env(path):
    out = {}
    for line in open(path):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def sh(cmd):
    r = subprocess.run(["bash", "-c", "set -o pipefail; " + cmd], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"{cmd.split('|')[0][:80]}... failed: {r.stderr.strip()[-300:]}")
    return r.stdout


def mysql(query):
    return sh(f"mysql -N -B -e {json.dumps(query)}")


def archive(src_cmd, name, out, password, stamp):
    """Streams a command's output into an encrypted, split 7z; returns the archive parts."""
    target = os.path.join(out, f"{stamp}_{name}.7z")
    sh(f"{src_cmd} | 7z a -si{name} -mx=5 -mhe=on -v{VOLUME} -p{json.dumps(password)} {json.dumps(target)} > /dev/null")
    return sorted(glob.glob(target + ".*"))


def send(app, chat, path, kind, attempts=10):
    size = os.path.getsize(path)
    wait = 30
    for _ in range(attempts):
        try:
            msg = app.send_document(chat, path, caption=f"{kind.capitalize()} backup: {os.path.basename(path)}")
            if msg and msg.document and msg.document.file_size == size:
                return True
        except FloodWait as e:
            time.sleep((e.value if isinstance(e.value, int) else wait) + 1)
            continue
        except (RPCError, OSError, TimeoutError) as e:
            print(f"upload {path} failed: {e!r}")
        time.sleep(wait)
        wait = min(wait * 2, 600)
    return False


def discord_ping(text):
    try:
        token = load_env(DISCORD_ENV)["DISCORD_BOT_TOKEN"]
        body = json.dumps({"content": f"<@{DISCORD_USER}> {text}", "allowed_mentions": {"users": [DISCORD_USER]}}).encode()
        req = urllib.request.Request(f"https://discord.com/api/v10/channels/{DISCORD_LOGS}/messages", data=body,
                                     headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                                              "User-Agent": "DiscordBot (temucraft-backup, 1.0)"})
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print(f"Discord ping failed: {e!r}")


def upload(app, chat, files, total, now, errors, kind):
    with app:
        sent = 0
        for f in files:
            if send(app, chat, f, kind):
                sent += 1
            else:
                errors.append(f"upload {os.path.basename(f)}")
        summary = (f"{kind.capitalize()} backup {now:%d/%m/%Y %H:%M}: {sent}/{len(files)} files, {total / 1048576:.0f} MB"
                   + ("" if not errors else f"\nProblems: {'; '.join(errors)}"))
        try:
            app.send_message(chat, summary)
        except Exception as e:
            print(f"summary failed: {e!r}")
    return sent


def main():
    env = load_env(ENV)
    pw = env["ARCHIVE_PASSWORD"]
    now = datetime.now(TZ)
    kind = "automatic" if os.environ.get("INVOCATION_ID") else "manual"   # systemd sets INVOCATION_ID for the timer
    stamp = now.strftime("%d_%m_%Y_%H-%M-%S")
    out = os.path.join(BACKUPS, f"{stamp}_{kind}")
    os.makedirs(out, exist_ok=True)
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    files, errors = [], []

    for db in DATABASES:
        try:
            files += archive(f"mysqldump --single-transaction --quick --routines --triggers --set-gtid-purged=OFF {db}",
                             f"{db}.sql", out, pw, stamp)
        except RuntimeError as e:
            errors.append(f"dump {db}: {e}")

    # binary logs: close the current one, archive every closed one, purge them later if everything uploaded
    binlogs, last_binlog = [], None
    try:
        mysql("FLUSH BINARY LOGS")
        names = [l.split("\t")[0] for l in mysql("SHOW BINARY LOGS").splitlines() if l]
        base = os.path.dirname(mysql("SELECT @@log_bin_basename").strip())
        last_binlog = names[-1]
        binlogs = [os.path.join(base, n) for n in names[:-1]]
        if binlogs:
            files += archive(f"tar -cf - -C {base} {' '.join(os.path.basename(b) for b in binlogs)}",
                             "binlogs.tar", out, pw, stamp)
    except RuntimeError as e:
        errors.append(f"binary logs: {e}")

    # journal since the last good run (everything the first time)
    since = state.get("journal_until")
    until = now.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    try:
        files += archive(f"journalctl --no-pager -o short-iso {'--since ' + json.dumps(since) if since else ''} "
                         f"--until {json.dumps(until)}", "journal.log", out, pw, stamp)
    except RuntimeError as e:
        errors.append(f"journal: {e}")

    total = sum(os.path.getsize(f) for f in files)
    sent = 0
    if files:
        app = Client("temucraft_backup", api_id=int(env["TELEGRAM_API_ID"]), api_hash=env["TELEGRAM_API_HASH"],
                     bot_token=env["TELEGRAM_BOT_TOKEN"], workdir=os.path.dirname(os.path.abspath(__file__)),
                     no_updates=True)
        try:
            sent = upload(app, int(env["TELEGRAM_CHAT_ID"]), files, total, now, errors, kind)
        except Exception as e:  # login or chat problems: a failure like any other, nothing gets purged
            errors.append(f"telegram: {e!r}"[:300])

    if errors:
        print("FAILED:", errors)
        discord_ping(f"{kind.capitalize()} backup to Telegram failed ({sent}/{len(files)} sent); nothing was purged. " + "; ".join(errors)[:1500])
        sys.exit(1)

    # everything is on Telegram: clear what was only kept for it, and old local copies
    if last_binlog and binlogs:
        mysql(f"PURGE BINARY LOGS TO '{last_binlog}'")
    sh(f"journalctl --vacuum-time={JOURNAL_KEEP} > /dev/null 2>&1 || true")
    state["journal_until"] = until
    json.dump(state, open(STATE, "w"))
    cutoff = now.replace(tzinfo=None) - timedelta(days=KEEP_DAYS)
    for d in glob.glob(os.path.join(BACKUPS, "*")):
        name = os.path.basename(d)
        for fmt, n in (("%d_%m_%Y", 10), ("%Y-%m-%d", 10)):   # DD_MM_YYYY_... now, YYYY-MM-DD before
            try:
                if datetime.strptime(name[:n], fmt) < cutoff:
                    shutil.rmtree(d)
                break
            except ValueError:
                continue
    print(f"backup ok: {sent} files, {total / 1048576:.0f} MB in {out}")


if __name__ == "__main__":
    main()
