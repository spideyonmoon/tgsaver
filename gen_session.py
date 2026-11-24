"""One-time helper: log in with your Telegram account and print a session string.

Run it interactively (it asks for your phone number and the login code):
    docker compose run --rm -it tgsaver python gen_session.py
or locally:
    pip install -r requirements.txt && python gen_session.py

The string is full access to your account. Put it only in Dokploy's
Environment tab (TG_SESSION_STRING) and never commit or share it.
"""

import asyncio
import os

from pyrogram import Client


async def main():
    api_id = int(os.environ.get("API_ID") or input("API_ID: ").strip())
    api_hash = os.environ.get("API_HASH") or input("API_HASH: ").strip()
    async with Client("gen", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
        print("\nSESSION_STRING:\n" + await app.export_session_string() + "\n")


if __name__ == "__main__":
    asyncio.run(main())
