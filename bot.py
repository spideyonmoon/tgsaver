"""Private Telegram saver.

Send the bot a t.me link from a chat your own account can read (restricted /
no-forward chats included). It downloads the media with your account into
DOWNLOAD_DIR/<chat title>/, where the rclone container picks it up and moves it
to Google Drive.

Message formats (one job per line):
    https://t.me/c/123456/45              -> that one message
    https://t.me/c/123456/45 20           -> 20 messages starting at 45
    https://t.me/c/123456/45 https://t.me/c/123456/80   -> 45..80 inclusive
Public links (https://t.me/channel/45) and topic links (.../topic/msg) work too.

Commands: /status, /cancel (stops the current job and clears the queue).
"""

import asyncio
import logging
import mimetypes
import os
import re
import shutil
import time
from dataclasses import dataclass, field

from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("tgsaver")

# ── config ──────────────────────────────────────────────────────────────────
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]


def clean_session_string(raw):
    """Undo common copy/paste damage: line wraps, spaces, quotes, the gen_session label."""
    s = re.sub(r"\s+", "", raw).strip("'\"")
    if ":" in s:  # e.g. "SESSION_STRING:AbC..." copied together with the label
        s = s.rsplit(":", 1)[1]
    return s


def session_string_problem(s):
    """None if it looks like a pyrofork/pyrogram session string, else a human explanation."""
    if not s:
        return "SESSION_STRING is empty."
    if not re.fullmatch(r"[A-Za-z0-9_-]+", s):
        bad = sorted(set(re.sub(r"[A-Za-z0-9_-]", "", s)))
        return f"SESSION_STRING contains characters a session string never has: {''.join(bad)!r}."
    if len(s) in (362, 351, 356):
        return None
    if s.startswith("1") and len(s) == 353:
        return "SESSION_STRING looks like a Telethon session; this bot needs a Pyrogram one from gen_session.py."
    return (f"SESSION_STRING is {len(s)} characters; a Pyrogram session is 362. It was probably cut off "
            "or wrapped when copied. Regenerate with gen_session.py and paste it as one line.")


SESSION_STRING = clean_session_string(os.environ["SESSION_STRING"])
OWNER_IDS = {int(x) for x in os.environ["OWNER_ID"].replace(",", " ").split()}
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/data/telegram")
INCOMPLETE_DIR = os.path.join(DOWNLOAD_DIR, ".incomplete")
DELAY_SECONDS = float(os.environ.get("DELAY_SECONDS", "2"))  # pause between files
MAX_BATCH = int(os.environ.get("MAX_BATCH", "2000"))
EDIT_EVERY = 6  # seconds between progress edits (Telegram rate-limits edits)

# ── link parsing ────────────────────────────────────────────────────────────
# t.me/c/<id>/<msg>, t.me/c/<id>/<topic>/<msg>, t.me/<user>/<msg>, t.me/<user>/<topic>/<msg>
LINK_RE = re.compile(
    r"(?:https?://)?(?:t|telegram)\.me/(?:(c)/(\d+)|([A-Za-z][\w]{3,}))/(?:\d+/)?(\d+)"
)


@dataclass
class Job:
    chat_ref: object  # int (-100…) for private chats, username for public ones
    first: int
    last: int
    status: Message = None
    cancelled: bool = False
    last_edit: float = field(default=0.0)

    @property
    def total(self):
        return self.last - self.first + 1


def _ref(match):
    is_private, private_id, username, msg_id = match.groups()
    chat = int(f"-100{private_id}") if is_private else username
    return chat, int(msg_id)


def parse_line(line):
    """Return (chat_ref, first, last) for one line, None if it has no link."""
    matches = list(LINK_RE.finditer(line))
    if not matches:
        return None
    chat, first = _ref(matches[0])
    if len(matches) >= 2:
        chat2, last = _ref(matches[1])
        if chat2 != chat:
            raise ValueError("both links in a range must be from the same chat")
    else:
        rest = LINK_RE.sub(" ", line).split()
        count = int(rest[0]) if rest and rest[0].isdigit() else 1
        if count < 1:
            raise ValueError("count must be at least 1")
        last = first + count - 1
    first, last = min(first, last), max(first, last)
    if last - first + 1 > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} messages per job")
    return chat, first, last


# ── file naming ─────────────────────────────────────────────────────────────
MEDIA_KINDS = ("video", "document", "audio", "animation", "voice", "video_note", "photo")
DEFAULT_EXT = {
    "video": ".mp4", "animation": ".mp4", "video_note": ".mp4",
    "audio": ".mp3", "voice": ".ogg", "photo": ".jpg", "document": ".bin",
}


def sanitize(name, limit=150):
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].rstrip(" .") or "untitled"


def media_of(m):
    for kind in MEDIA_KINDS:
        obj = getattr(m, kind, None)
        if obj:
            return kind, obj
    return None, None


def file_name_for(m, kind, media):
    """'<msg id> - <original name or first caption line><ext>' so files sort in post order."""
    original = getattr(media, "file_name", None) or ""
    stem, ext = os.path.splitext(original)
    if not ext:
        mime = getattr(media, "mime_type", None)
        ext = (mimetypes.guess_extension(mime) if mime else None) or DEFAULT_EXT[kind]
    if not stem:
        caption = (m.caption or "").strip()
        stem = caption.splitlines()[0] if caption else kind
    return f"{m.id} - {sanitize(stem)}{ext}"


# ── helpers ─────────────────────────────────────────────────────────────────
def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def eta(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60}:{seconds % 60:02d}"


async def edit(job, text, force=False):
    now = time.monotonic()
    if not force and now - job.last_edit < EDIT_EVERY:
        return
    job.last_edit = now
    try:
        await job.status.edit_text(text)
    except MessageNotModified:
        pass
    except FloodWait as e:
        job.last_edit = now + e.value  # back off, don't block the download
    except Exception as e:  # never let a status edit abort a download
        log.warning("status edit failed: %s", e)


async def with_floodwait(fn, *args, **kwargs):
    for _ in range(5):
        try:
            return await fn(*args, **kwargs)
        except FloodWait as e:
            log.warning("FloodWait %ss", e.value)
            await asyncio.sleep(e.value + 1)
    return await fn(*args, **kwargs)


# ── the app ─────────────────────────────────────────────────────────────────
class Saver:
    def __init__(self):
        self.bot = Client("tgsaver-bot", api_id=API_ID, api_hash=API_HASH,
                          bot_token=BOT_TOKEN, in_memory=True)
        self.user = Client("tgsaver-user", api_id=API_ID, api_hash=API_HASH,
                           session_string=SESSION_STRING, in_memory=True)
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.current: Job | None = None

        owner = filters.private & filters.user(list(OWNER_IDS))
        self.bot.add_handler(MessageHandler(self.on_start, owner & filters.command(["start", "help"])))
        self.bot.add_handler(MessageHandler(self.on_status, owner & filters.command("status")))
        self.bot.add_handler(MessageHandler(self.on_cancel, owner & filters.command(["cancel", "stop"])))
        self.bot.add_handler(MessageHandler(self.on_text, owner & filters.text & ~filters.regex(r"^/")))

    # ── handlers ──
    async def on_start(self, _, m: Message):
        await m.reply_text(__doc__.split("\n", 2)[2].strip(), disable_web_page_preview=True)

    async def on_status(self, _, m: Message):
        job = self.current
        busy = f"Working on {job.total} message(s) from {job.chat_ref}" if job else "Idle"
        await m.reply_text(f"{busy}\nQueued jobs: {self.queue.qsize()}\n"
                           f"Free disk: {human(shutil.disk_usage(DOWNLOAD_DIR).free)}")

    async def on_cancel(self, _, m: Message):
        dropped = 0
        while not self.queue.empty():
            self.queue.get_nowait().cancelled = True
            self.queue.task_done()
            dropped += 1
        if self.current:
            self.current.cancelled = True
        await m.reply_text(f"Cancelling current job{' and ' + str(dropped) + ' queued' if dropped else ''}.")

    async def on_text(self, _, m: Message):
        jobs, errors = [], []
        for line in m.text.splitlines():
            try:
                parsed = parse_line(line)
            except ValueError as e:
                errors.append(f"{line.strip()[:60]}: {e}")
                continue
            if parsed:
                jobs.append(Job(*parsed))
        if errors:
            await m.reply_text("Skipped:\n" + "\n".join(errors))
        if not jobs and not errors:
            await m.reply_text("Send a t.me message link (optionally followed by a count, or a second link for a range).")
        for job in jobs:
            ahead = self.queue.qsize() + (1 if self.current else 0)
            job.status = await m.reply_text(
                f"Queued {job.total} message(s)" + (f", {ahead} job(s) ahead" if ahead else ""))
            await self.queue.put(job)

    # ── work ──
    async def resolve(self, ref, job=None):
        try:
            return await self.user.get_chat(ref)
        except (RPCError, KeyError, ValueError):
            pass
        # A session string starts with an empty peer cache, so a private chat ID from a
        # link is unknown until we've seen it in the dialog list. Walk the dialogs (most
        # recent first) and stop as soon as the chat turns up; it stays cached afterwards.
        if job:
            await edit(job, "Looking up the chat in your dialogs…", force=True)
        async for dialog in self.user.get_dialogs():
            if dialog.chat.id == ref or (dialog.chat.username or "").lower() == str(ref).lower():
                break
        return await self.user.get_chat(ref)

    async def worker(self):
        while True:
            job = await self.queue.get()
            if job.cancelled:
                continue
            self.current = job
            try:
                await self.run(job)
            except Exception as e:  # keep the worker alive whatever happens
                log.exception("job failed")
                await edit(job, f"Failed: {type(e).__name__}: {e}", force=True)
            finally:
                self.current = None
                self.queue.task_done()

    async def run(self, job: Job):
        try:
            chat = await self.resolve(job.chat_ref, job)
        except Exception as e:
            await edit(job, f"Can't open that chat with your account ({type(e).__name__}). "
                            "Is the account a member?", force=True)
            return
        title = chat.title or chat.username or str(chat.id)
        folder = sanitize(title, 100)
        saved = skipped = failed = 0
        ids = list(range(job.first, job.last + 1))

        for start in range(0, len(ids), 100):
            if job.cancelled:
                break
            msgs = await with_floodwait(self.user.get_messages, chat.id, ids[start:start + 100])
            for m in msgs if isinstance(msgs, list) else [msgs]:
                if job.cancelled:
                    break
                kind, media = media_of(m) if m and not m.empty else (None, None)
                if not media:
                    skipped += 1
                    continue
                pos = f"[{saved + skipped + failed + 1}/{job.total}] {title}"
                try:
                    ok = await self.download(job, m, kind, media, folder, pos)
                except Exception as e:
                    log.exception("download of %s/%s failed", chat.id, m.id)
                    await edit(job, f"{pos}\nError on {m.id}: {e}", force=True)
                    ok = False
                if ok is None:
                    skipped += 1
                elif ok:
                    saved += 1
                else:
                    failed += 1
                await asyncio.sleep(DELAY_SECONDS)

        head = "Cancelled" if job.cancelled else "Done"
        await edit(job, f"{head}: {title}\nSaved {saved} · skipped {skipped} (no media/exists) · failed {failed}\n"
                        f"→ {os.path.join(DOWNLOAD_DIR, folder)}\nrclone moves it to Drive on its next round.",
                   force=True)

    async def download(self, job, m, kind, media, folder, pos):
        """True = saved, None = already there, False = failed/cancelled."""
        name = file_name_for(m, kind, media)
        final_dir = os.path.join(DOWNLOAD_DIR, folder)
        final_path = os.path.join(final_dir, name)
        if os.path.exists(final_path):
            return None
        tmp_path = os.path.join(INCOMPLETE_DIR, f"{m.chat.id}_{m.id}{os.path.splitext(name)[1]}")
        size = getattr(media, "file_size", 0) or 0
        started = time.monotonic()
        await edit(job, f"{pos}\n⬇️ {name}\n0 / {human(size)}", force=True)

        async def progress(current, total):
            if job.cancelled:
                self.user.stop_transmission()
            elapsed = max(time.monotonic() - started, 0.1)
            speed = current / elapsed
            left = (total - current) / speed if speed else 0
            await edit(job, f"{pos}\n⬇️ {name}\n{human(current)} / {human(total)} · "
                            f"{human(speed)}/s · ETA {eta(left)}")

        os.makedirs(INCOMPLETE_DIR, exist_ok=True)
        path = await with_floodwait(self.user.download_media, m, file_name=tmp_path, progress=progress)
        if not path or job.cancelled:
            if path and os.path.exists(path):
                os.remove(path)
            return False
        # Only now does the file appear where rclone looks, so it never ships a partial file.
        # rclone's --delete-empty-src-dirs can remove the chat folder between files; recreate it.
        for _ in range(3):
            os.makedirs(final_dir, exist_ok=True)
            try:
                os.replace(path, final_path)
                break
            except FileNotFoundError:
                await asyncio.sleep(1)
        else:
            os.replace(path, final_path)
        log.info("saved %s", final_path)
        return True

    async def prepare_dirs(self):
        # The rclone container creates /data/telegram owned by 1000:1000; wait for it if needed.
        while True:
            try:
                os.makedirs(INCOMPLETE_DIR, exist_ok=True)
                break
            except PermissionError:
                log.warning("%s not writable yet, retrying in 10s", DOWNLOAD_DIR)
                await asyncio.sleep(10)
        for leftover in os.listdir(INCOMPLETE_DIR):  # partials from a crash/restart
            os.remove(os.path.join(INCOMPLETE_DIR, leftover))

    async def main(self):
        await self.prepare_dirs()
        await self.user.start()
        me = await self.user.get_me()
        log.info("user client: %s (%s)", me.first_name, me.id)
        await self.bot.start()
        asyncio.create_task(self.worker())
        for owner in OWNER_IDS:
            try:
                await self.bot.send_message(owner, "tgsaver is up. Send a link.")
            except RPCError:
                log.warning("can't message owner %s yet (send the bot /start once)", owner)
        log.info("ready")
        await idle()
        await self.bot.stop()
        await self.user.stop()


async def _amain():
    problem = session_string_problem(SESSION_STRING)
    if problem:
        log.error(problem)
        await asyncio.sleep(60)  # don't spin in a tight restart loop
        raise SystemExit(1)
    # Build the clients inside the running loop so pyrogram binds to it.
    await Saver().main()


if __name__ == "__main__":
    asyncio.run(_amain())
