"""jobwatch: watches a SimplifyJobs-style listings.json and posts new roles to Discord."""

import asyncio
import datetime
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("jobwatch")


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ[key] = value


_load_dotenv()


DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
LISTINGS_URL = os.environ.get(
    "LISTINGS_URL",
    "https://github.com/speedyapply/2027-SWE-College-Jobs",
)
POLL_MINUTES = float(os.environ.get("POLL_MINUTES", "30"))
DB_PATH = os.environ.get("DB_PATH", "seen.db")
BOOTSTRAP_POST_LIMIT = int(os.environ.get("BOOTSTRAP_POST_LIMIT", "10"))
INTERNSHIPS_ONLY = os.environ.get("INTERNSHIPS_ONLY", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
MIN_DATE_POSTED_RAW = os.environ.get("MIN_DATE_POSTED", "")

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

_INTERNSHIP_PHRASES = (
    "intern",
    "internship",
    "co-op",
    "co op",
)

_FULL_TIME_PHRASES = (
    "full time",
    "full-time",
    "new grad",
    "new graduate",
)

POST_DELAY_SECONDS = 1.0
TIER_COMMAND_LIMIT = 5
_TIER_ALIASES = {
    "faang": "FAANG+",
    "faang+": "FAANG+",
    "fang": "FAANG+",
    "quant": "Quant",
    "other": "Other",
}


def _parse_cutoff(value: str) -> datetime.datetime | None:
    raw = value.strip()
    if not raw:
        return None

    if raw.isdigit():
        return datetime.datetime.fromtimestamp(int(raw), tz=datetime.timezone.utc)

    try:
        cutoff = datetime.datetime.fromisoformat(raw)
    except ValueError:
        log.warning("Could not parse MIN_DATE_POSTED=%r; ignoring cutoff", value)
        return None

    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=datetime.timezone.utc)
    return cutoff.astimezone(datetime.timezone.utc)


MIN_DATE_POSTED = _parse_cutoff(MIN_DATE_POSTED_RAW)


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


def has_bootstrapped(db_path: str) -> bool:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'bootstrap_done'").fetchone()
        return row is not None and row[0] == "1"


def set_bootstrapped(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('bootstrap_done', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        conn.commit()


def get_meta(db_path: str, key: str) -> str | None:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row is not None else None


def set_meta(db_path: str, key: str, value: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
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


def _role_text(role: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in role.values():
        if isinstance(value, str):
            parts.append(value.lower())
        elif isinstance(value, list):
            parts.extend(str(item).lower() for item in value)
    return " ".join(parts)


def _role_date_posted(role: dict[str, Any]) -> datetime.datetime | None:
    date_posted = role.get("date_posted")
    if isinstance(date_posted, (int, float)):
        return datetime.datetime.fromtimestamp(date_posted, tz=datetime.timezone.utc)
    if isinstance(date_posted, str) and date_posted.strip().isdigit():
        return datetime.datetime.fromtimestamp(int(date_posted.strip()), tz=datetime.timezone.utc)
    return None


def _role_sort_key(role: dict[str, Any]) -> tuple[int, float]:
    date_posted = _role_date_posted(role)
    if date_posted is None:
        return (0, 0.0)
    return (1, date_posted.timestamp())


def _markdown_link(cell: str) -> tuple[str, str | None]:
    # Apply badges are often nested Markdown links, for example:
    # [![Apply](badge-image)](actual-application-url). The final URL is the
    # outer link and is the one applicants need.
    markdown_urls = re.findall(r"\]\(([^)]+)\)", cell)
    if markdown_urls:
        match = re.search(r"\[([^\]]+)\]\(([^)]+)\)", cell)
        label = match.group(1) if match else cell
        label = re.sub(r"!\[([^\]]*)\]", r"\1", label)
        return _clean_listing_text(label), html.unescape(markdown_urls[-1].strip())

    # Some versions of the source table use HTML instead of Markdown links.
    # Discord does not render HTML, so extract the href and keep only the text.
    anchor = re.search(
        r"<a\b[^>]*\bhref\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a>",
        cell,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if anchor:
        return _clean_listing_text(anchor.group(3)), html.unescape(anchor.group(2).strip())

    return _clean_listing_text(cell), None


def _clean_listing_text(value: str) -> str:
    """Convert source Markdown/HTML formatting into text suitable for Discord."""
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[*`]", "", value)
    return html.unescape(value).strip()


def _parse_markdown_listings(markdown: str, source_url: str) -> list[dict[str, Any]]:
    """Parse the repository's markdown job tables into the bot's role shape."""
    roles: list[dict[str, Any]] = []
    tier: str | None = None
    for line in markdown.splitlines():
        heading = re.fullmatch(r"\s*#{3,}\s+(.+?)\s*", line)
        if heading:
            tier = _TIER_ALIASES.get(_clean_listing_text(heading.group(1)).lower())
            continue
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 5 or set(cells[0]) <= {"-", ":"}:
            continue

        company, company_url = _markdown_link(cells[0])
        title, _ = _markdown_link(cells[1])
        if company.lower() == "company" or title.lower() == "position":
            continue
        location = cells[2]
        age_cell = cells[-1]
        age_match = re.fullmatch(r"(\d+)d", age_cell)
        age_days = int(age_match.group(1)) if age_match else 0
        apply_url = None
        fallback_url = None
        for cell in cells[3:-1]:
            _, candidate_url = _markdown_link(cell)
            if not candidate_url:
                continue
            if "apply" in cell.lower():
                apply_url = urljoin(source_url, candidate_url)
                break
            if candidate_url.startswith(("http://", "https://")):
                fallback_url = urljoin(source_url, candidate_url)
        if not apply_url:
            apply_url = fallback_url
        if not apply_url:
            apply_url = company_url
        if not title or not company:
            continue

        role_id = hashlib.sha256(
            f"{company}|{title}|{location}|{apply_url}".encode("utf-8")
        ).hexdigest()
        posted = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=age_days)
        roles.append(
            {
                "id": role_id,
                "company_name": company,
                "title": title,
                "locations": [location] if location else [],
                "url": apply_url,
                "date_posted": posted.timestamp(),
                "tier": tier,
                "active": True,
                "is_visible": True,
            }
        )
    return roles


def _github_readme_url(url: str) -> str:
    match = re.match(r"https://github\.com/([^/]+/[^/]+)(?:/.*)?/?$", url)
    if not match:
        return url
    return f"https://raw.githubusercontent.com/{match.group(1)}/main/README.md"


def passes_filters(role: dict[str, Any]) -> bool:
    title = (role.get("title") or "").lower()
    locations = [str(loc).lower() for loc in (role.get("locations") or [])]
    sponsorship = (role.get("sponsorship") or "").lower()
    role_text = _role_text(role)

    if TITLE_KEYWORDS and not any(kw in title for kw in TITLE_KEYWORDS):
        return False

    if INTERNSHIPS_ONLY:
        if not any(phrase in role_text or phrase in title for phrase in _INTERNSHIP_PHRASES):
            return False
        if any(phrase in role_text or phrase in title for phrase in _FULL_TIME_PHRASES):
            return False

    if LOCATIONS_FILTER and not any(
        loc_filter in loc for loc_filter in LOCATIONS_FILTER for loc in locations
    ):
        return False

    if SPONSORSHIP_ONLY and any(phrase in sponsorship for phrase in _NO_SPONSORSHIP_PHRASES):
        return False

    if MIN_DATE_POSTED is not None:
        date_posted = _role_date_posted(role)
        if date_posted is None or date_posted < MIN_DATE_POSTED:
            return False

    return True


def build_embed(role: dict) -> discord.Embed:
    title = _clean_listing_text(str(role.get("title") or "Unknown title"))
    company = _clean_listing_text(str(role.get("company_name") or "Unknown company"))
    url = role.get("url") or None
    locations = [_clean_listing_text(str(location)) for location in (role.get("locations") or [])]
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
        self.tree = app_commands.CommandTree(self)
        # Register bound methods. Decorating methods directly on this client
        # class leaves ``self`` in Discord's command signature, which can
        # disagree with the signature registered remotely.
        self.tree.command(
            name="faang", description="Show the five newest FAANG+ listings"
        )(self.faang)
        self.tree.command(
            name="quant", description="Show the five newest Quant listings"
        )(self.quant)

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        init_db(DB_PATH)
        self.poll_listings.change_interval(minutes=POLL_MINUTES)
        self.poll_listings.start()
        await self.tree.sync()

    async def close(self) -> None:
        if self.http_session is not None:
            await self.http_session.close()
        await super().close()

    async def fetch_listings(self) -> list[dict] | None:
        fetch_url = _github_readme_url(LISTINGS_URL)
        try:
            async with self.http_session.get(fetch_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                body = await resp.text()
        except Exception as exc:
            log.warning("Failed to fetch listings from %s: %s", LISTINGS_URL, exc)
            return None

        try:
            data = json.loads(body)
        except ValueError:
            data = _parse_markdown_listings(body, fetch_url)

        if not isinstance(data, list):
            log.warning("Unexpected listings payload shape (expected a list): %r", type(data))
            return None

        return data

    async def send_tier_listings(
        self, interaction: discord.Interaction, tier_name: str
    ) -> None:
        """Reply to a slash command with the newest listings in one source tier."""
        await interaction.response.defer(thinking=True)
        roles = await self.fetch_listings()
        if roles is None:
            await interaction.followup.send(
                "I couldn't load the listings right now. Please try again shortly.",
                ephemeral=True,
            )
            return

        matching_roles = [
            role
            for role in roles
            if role.get("tier") == tier_name
            and role.get("active") is not False
            and role.get("is_visible") is not False
            and passes_filters(role)
        ]
        newest = sorted(matching_roles, key=_role_sort_key, reverse=True)[:TIER_COMMAND_LIMIT]
        if not newest:
            await interaction.followup.send(
                f"No current {tier_name} listings match this bot's filters.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Newest {tier_name} listings ({len(newest)}):"
        )
        for role in newest:
            await interaction.followup.send(embed=build_embed(role))

    async def faang(self, interaction: discord.Interaction) -> None:
        await self.send_tier_listings(interaction, "FAANG+")

    async def quant(self, interaction: discord.Interaction) -> None:
        await self.send_tier_listings(interaction, "Quant")

    @tasks.loop(minutes=30)
    async def poll_listings(self) -> None:
        roles = await self.fetch_listings()
        if roles is None:
            return
        roles = sorted(roles, key=_role_sort_key, reverse=True)

        seeding = not is_seeded(DB_PATH)
        # Databases created before source tracking have no ``listings_source``
        # value. Treat that just like a source change so historical listings
        # cannot bypass the bootstrap cap.
        source_changed = get_meta(DB_PATH, "listings_source") != LISTINGS_URL
        bootstrapping = (
            (source_changed or not has_bootstrapped(DB_PATH))
            and BOOTSTRAP_POST_LIMIT > 0
        )
        if seeding:
            log.info("Cold start: seeding %d existing roles without alerting", len(roles))
        if source_changed:
            log.info("Listings source changed; applying the bootstrap post limit")
        if bootstrapping:
            log.info(
                "Bootstrap test enabled: will post up to %d qualifying roles on this run",
                BOOTSTRAP_POST_LIMIT,
            )

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
            # A role already in the database is never posted again, including
            # during a source transition or bootstrap run.
            if has_seen(DB_PATH, role_id):
                continue

            # Record before alerting: a crash mid-post can't cause a duplicate alert later.
            mark_seen(DB_PATH, role_id)
            new_count += 1

            if seeding and not bootstrapping:
                continue

            if role.get("active") is False or role.get("is_visible") is False:
                continue
            if not passes_filters(role):
                continue

            if bootstrapping and posted_count >= BOOTSTRAP_POST_LIMIT:
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
        if bootstrapping:
            set_bootstrapped(DB_PATH)
            log.info("Bootstrap test complete: posted %d roles", posted_count)
        else:
            log.info("Poll complete: %d new roles, %d posted", new_count, posted_count)
        set_meta(DB_PATH, "listings_source", LISTINGS_URL)

    @poll_listings.error
    async def poll_listings_error(self, error: BaseException) -> None:
        log.warning("poll_listings loop raised (will continue on next cycle): %s", error)


def main() -> None:
    intents = discord.Intents.default()
    client = JobWatchClient(intents=intents)
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
