"""jobwatch: watches a SimplifyJobs-style listings.json and posts new roles to Discord."""

import asyncio
import datetime
import logging
import os
import sqlite3
import time
from contextlib import closing

import aiohttp
import discord
from discord.ext import tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("jobwatch")


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
LISTINGS_URL = os.environ.get(
    "LISTINGS_URL",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
)
POLL_MINUTES = float(os.environ.get("POLL_MINUTES", "30"))
DB_PATH = os.environ.get("DB_PATH", "seen.db")

TITLE_KEYWORDS = _env_list("TITLE_KEYWORDS")
LOCATIONS_FILTER = _env_list("LOCATIONS")
SPONSORSHIP_ONLY = os.environ.get("SPONSORSHIP_ONLY", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Phrases that indicate a role explicitly will not sponsor / requires citizenship.
_NO_SPONSORSHIP_PHRASES = (
    "no sponsorship",
    "not provide sponsorship",
    "unable to sponsor",
    "does not sponsor",
    "u.s. citizenship",
    "us citizenship",
    "citizenship required",
    "security clearance",
    "must be a us citizen",
    "must be a u.s. citizen",
)

POST_DELAY_SECONDS = 1.0


def init_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_roles (id TEXT PRIMARY KEY, seen_at INTEGER NOT NULL)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()


def is_seeded(db_path: str) -> bool:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'seeded'").fetchone()
        return row is not None and row[0] == "1"


def set_seeded(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('seeded', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        conn.commit()


def has_seen(db_path: str, role_id: str) -> bool:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT 1 FROM seen_roles WHERE id = ?", (role_id,)).fetchone()
        return row is not None


def mark_seen(db_path: str, role_id: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_roles (id, seen_at) VALUES (?, ?)",
            (role_id, int(time.time())),
        )
        conn.commit()


def passes_filters(role: dict) -> bool:
    title = (role.get("title") or "").lower()
    locations = [str(loc).lower() for loc in (role.get("locations") or [])]
    sponsorship = (role.get("sponsorship") or "").lower()

    if TITLE_KEYWORDS and not any(kw in title for kw in TITLE_KEYWORDS):
        return False

    if LOCATIONS_FILTER and not any(
        loc_filter in loc for loc_filter in LOCATIONS_FILTER for loc in locations
    ):
        return False

    if SPONSORSHIP_ONLY and any(phrase in sponsorship for phrase in _NO_SPONSORSHIP_PHRASES):
        return False

    return True


def build_embed(role: dict) -> discord.Embed:
    title = role.get("title") or "Unknown title"
    company = role.get("company_name") or "Unknown company"
    url = role.get("url") or None
    locations = role.get("locations") or []
    date_posted = role.get("date_posted")

    embed = discord.Embed(
        title=f"{company} — {title}",
        url=url,
        color=discord.Color.blue(),
    )
    if locations:
        embed.add_field(name="Locations", value=", ".join(locations), inline=False)
    sponsorship = role.get("sponsorship")
    if sponsorship:
        embed.add_field(name="Sponsorship", value=sponsorship, inline=False)
    if isinstance(date_posted, (int, float)):
        embed.timestamp = datetime.datetime.fromtimestamp(date_posted, tz=datetime.timezone.utc)
    return embed


class JobWatchClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        init_db(DB_PATH)
        self.poll_listings.change_interval(minutes=POLL_MINUTES)
        self.poll_listings.start()

    async def close(self) -> None:
        if self.http_session is not None:
            await self.http_session.close()
        await super().close()

    async def fetch_listings(self) -> list[dict] | None:
        try:
            async with self.http_session.get(LISTINGS_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except Exception as exc:
            log.warning("Failed to fetch listings from %s: %s", LISTINGS_URL, exc)
            return None

        if not isinstance(data, list):
            log.warning("Unexpected listings payload shape (expected a list): %r", type(data))
            return None

        return data

    @tasks.loop(minutes=30)
    async def poll_listings(self) -> None:
        roles = await self.fetch_listings()
        if roles is None:
            return

        seeding = not is_seeded(DB_PATH)
        if seeding:
            log.info("Cold start: seeding %d existing roles without alerting", len(roles))

        channel = self.get_channel(DISCORD_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(DISCORD_CHANNEL_ID)
            except Exception as exc:
                log.warning("Could not resolve Discord channel %s: %s", DISCORD_CHANNEL_ID, exc)
                return

        new_count = 0
        posted_count = 0
        for role in roles:
            role_id = role.get("id")
            if not role_id:
                continue
            if has_seen(DB_PATH, role_id):
                continue

            # Record before alerting: a crash mid-post can't cause a duplicate alert later.
            mark_seen(DB_PATH, role_id)
            new_count += 1

            if seeding:
                continue

            if role.get("active") is False or role.get("is_visible") is False:
                continue
            if not passes_filters(role):
                continue

            try:
                await channel.send(embed=build_embed(role))
                posted_count += 1
                await asyncio.sleep(POST_DELAY_SECONDS)
            except Exception as exc:
                log.warning("Failed to post role %s: %s", role_id, exc)

        if seeding:
            set_seeded(DB_PATH)
            log.info("Seeding complete: recorded %d role IDs", new_count)
        else:
            log.info("Poll complete: %d new roles, %d posted", new_count, posted_count)

    @poll_listings.error
    async def poll_listings_error(self, error: BaseException) -> None:
        log.warning("poll_listings loop raised (will continue on next cycle): %s", error)


def main() -> None:
    intents = discord.Intents.default()
    client = JobWatchClient(intents=intents)
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
