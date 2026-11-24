# tgsaver

A one-person Telegram bot, stripped down from Save-Restricted-Content-Bot-v3.
You send it a `t.me` link and it downloads that post's media with **your own
account**, which works on no-forward chats too, into `/data/telegram/<chat title>/` on the
shared `data` volume. The existing rclone container then moves it to
`gdrive:Telegram`.

```
you ──link──▶ bot ──(your account)──▶ /data/telegram/.incomplete/…   (partial)
                                       └─ moved when complete ─▶ /data/telegram/<chat>/<msg id> - <name>.mp4
rclone (every 5 min) ── rclone move ──▶ gdrive:Telegram/<chat>/…
```

## Using it

Send the bot (one job per line):

| Message | Does |
|---|---|
| `https://t.me/c/1234567890/45` | that one post |
| `https://t.me/c/1234567890/45 20` | 20 posts starting at 45 |
| `https://t.me/c/1234567890/45 https://t.me/c/1234567890/80` | posts 45–80 |
| `https://t.me/somechannel/12` | public channels and topic links work too |
| `/status` | current job, queue, free disk |
| `/cancel` | stop current job and clear the queue |

Posts without media are skipped. Files already sitting in the folder are skipped.
Files are named `<msg id> - <original name or first caption line>.<ext>` so
they sort in post order on Drive.

Only the IDs in `TG_OWNER_ID` can use the bot. Everyone else is ignored.

## One-time setup

1. **API id/hash:** https://my.telegram.org → *API development tools*.
2. **Bot token:** create a bot with @BotFather, then send it `/start` once so it can message you.
3. **Your user id:** e.g. from @userinfobot.
4. **Session string:** log in with your account once:
   ```sh
   docker compose run --rm -it tgsaver python gen_session.py
   ```
   (or locally: `pip install -r requirements.txt && python gen_session.py`).
   It asks for your phone and the login code and saves the string (362 characters,
   one line) to `session_string.txt`. Copy it from that file, not from the terminal,
   and delete the file afterwards. **Anyone
   holding this string has full access to your Telegram account.** Keep it only in
   Dokploy's Environment tab.
5. Add to Dokploy's Environment tab:
   `TG_API_ID`, `TG_API_HASH`, `TG_BOT_TOKEN`, `TG_SESSION_STRING`, `TG_OWNER_ID`.

## Compose changes (Dokploy stack)

### 1. New service

```yaml
  # Telegram saver bot: downloads with your account into /data/telegram for rclone to ship.
  tgsaver:
    build: https://github.com/spideyonmoon/tgsaver.git#main
    environment:
      - API_ID=${TG_API_ID}
      - API_HASH=${TG_API_HASH}
      - BOT_TOKEN=${TG_BOT_TOKEN}
      - SESSION_STRING=${TG_SESSION_STRING}
      - OWNER_ID=${TG_OWNER_ID}
      - DOWNLOAD_DIR=/data/telegram
      - TZ=Asia/Dhaka
    volumes:
      - data:/data
    depends_on:
      - rclone
    restart: unless-stopped
```

The container runs as `1000:1000` like the rest of the stack. It needs no ports
because it polls Telegram. It also shows up in FileBrowser under `telegram/`.

Optional env:
- `PARALLEL`: connections per file, default `4`. Files of 20 MB or more are
  split into that many byte ranges downloaded at once. A single stream is capped
  by round-trip time (~3–4 MB/s), so this is the speed knob. `1` turns it off.
  Going past ~8 makes Telegram more likely to slow you down.
- `DELAY_SECONDS`: pause between files, default `2`.
- `MAX_BATCH`: most posts per job, default `2000`.

### 2. rclone service: three small additions to its `command`

Create and own the folder (extend the existing `mkdir`/`chown` lines):

```sh
        mkdir -p /data/torrents/manual /data/torrents/incomplete /data/zips/.done /data/zips/.done-aria2 /data/telegram
        chown 1000:1000 /data/torrents /data/torrents/manual /data/torrents/incomplete /data/telegram
```

Next to the other `rclone mkdir` lines:

```sh
        rclone mkdir gdrive:Telegram
```

Add this function, for example after `cleanup_aria2() { … }`:

```sh
        # Move finished Telegram downloads to Drive, keeping the <chat title>/ folders.
        # The bot only places complete files here (partials stay in the hidden .incomplete/),
        # so they can go straight away. Local copies are deleted once uploaded.
        ship_telegram() {
          rclone move /data/telegram gdrive:Telegram \
            --exclude ".incomplete/**" --min-age 1m --delete-empty-src-dirs \
            --drive-chunk-size 64M -v
        }
```

Then call it in the loop:

```sh
        while true; do
          ship /data/torrents/manual gdrive:Manual
          ship_aria2
          ship_telegram
          cleanup manual
          cleanup_aria2
          sleep 300
        done
```

There's no `$` in these lines, so the compose `$$` escaping doesn't apply.

## Notes

- The account must be able to see the chat (be a member of private ones).
- Telegram may throttle heavy downloading (FloodWait). The bot waits it out
  automatically. Large batches are fine but not instant.
- On restart, any partial file in `.incomplete/` is deleted and you resend that link.
