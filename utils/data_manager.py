"""
utils/data_manager.py
Thread-safe async JSON I/O for tickets, stats, and ratings.
All writes go through asyncio locks to prevent race conditions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("PupPet.data")

DATA_DIR = Path(__file__).parent.parent / "data"


class DataManager:
    """Manages all JSON data files with async read/write and locking."""

    FILES = {
        "tickets": DATA_DIR / "tickets.json",
        "stats":   DATA_DIR / "stats.json",
        "ratings": DATA_DIR / "ratings.json",
    }

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {k: asyncio.Lock() for k in self.FILES}
        self._cache: dict[str, dict] = {}
        DATA_DIR.mkdir(exist_ok=True)
        # Eager load on startup (sync — called before event loop fully running)
        for name, path in self.FILES.items():
            if path.exists():
                try:
                    self._cache[name] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    log.error("Corrupt JSON in %s — resetting.", path)
                    self._cache[name] = {}
            else:
                path.write_text("{}", encoding="utf-8")
                self._cache[name] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _read(self, name: str) -> dict:
        async with self._locks[name]:
            return dict(self._cache.get(name, {}))

    async def _write(self, name: str, data: dict) -> None:
        async with self._locks[name]:
            self._cache[name] = data
            loop = asyncio.get_running_loop()
            path = self.FILES[name]
            text = json.dumps(data, indent=2, ensure_ascii=False)
            await loop.run_in_executor(None, lambda: path.write_text(text, encoding="utf-8"))

    # ------------------------------------------------------------------
    # Ticket CRUD
    # ------------------------------------------------------------------
    async def get_tickets(self) -> dict[str, Any]:
        return await self._read("tickets")

    async def get_ticket(self, channel_id: int) -> dict | None:
        tickets = await self._read("tickets")
        return tickets.get(str(channel_id))

    async def save_ticket(self, channel_id: int, data: dict) -> None:
        tickets = await self._read("tickets")
        tickets[str(channel_id)] = data
        await self._write("tickets", tickets)

    async def delete_ticket(self, channel_id: int) -> None:
        tickets = await self._read("tickets")
        tickets.pop(str(channel_id), None)
        await self._write("tickets", tickets)

    async def get_user_ticket(self, user_id: int) -> dict | None:
        """Return ticket data if the user has an active ticket."""
        tickets = await self._read("tickets")
        for data in tickets.values():
            if data.get("owner_id") == user_id:
                return data
        return None

    async def update_ticket_field(self, channel_id: int, **fields) -> None:
        tickets = await self._read("tickets")
        key = str(channel_id)
        if key in tickets:
            tickets[key].update(fields)
            await self._write("tickets", tickets)

    # ------------------------------------------------------------------
    # Stats CRUD
    # ------------------------------------------------------------------
    async def get_all_stats(self) -> dict[str, Any]:
        return await self._read("stats")

    async def get_staff_stats(self, user_id: int) -> dict:
        stats = await self._read("stats")
        return stats.get(str(user_id), {
            "claimed": 0,
            "closed": 0,
            "total_response_time": 0,
            "response_count": 0,
            "total_rating": 0,
            "rating_count": 0,
        })

    async def save_staff_stats(self, user_id: int, data: dict) -> None:
        stats = await self._read("stats")
        stats[str(user_id)] = data
        await self._write("stats", stats)

    async def increment_stat(self, user_id: int, field: str, amount: int = 1) -> None:
        stats = await self._read("stats")
        uid = str(user_id)
        if uid not in stats:
            stats[uid] = {
                "claimed": 0, "closed": 0,
                "total_response_time": 0, "response_count": 0,
                "total_rating": 0, "rating_count": 0,
            }
        stats[uid][field] = stats[uid].get(field, 0) + amount
        await self._write("stats", stats)

    async def add_response_time(self, user_id: int, seconds: float) -> None:
        stats = await self._read("stats")
        uid = str(user_id)
        if uid not in stats:
            stats[uid] = {
                "claimed": 0, "closed": 0,
                "total_response_time": 0, "response_count": 0,
                "total_rating": 0, "rating_count": 0,
            }
        stats[uid]["total_response_time"] = stats[uid].get("total_response_time", 0) + seconds
        stats[uid]["response_count"] = stats[uid].get("response_count", 0) + 1
        await self._write("stats", stats)

    async def add_rating(self, user_id: int, rating: int) -> None:
        stats = await self._read("stats")
        uid = str(user_id)
        if uid not in stats:
            stats[uid] = {
                "claimed": 0, "closed": 0,
                "total_response_time": 0, "response_count": 0,
                "total_rating": 0, "rating_count": 0,
            }
        stats[uid]["total_rating"] = stats[uid].get("total_rating", 0) + rating
        stats[uid]["rating_count"] = stats[uid].get("rating_count", 0) + 1
        await self._write("stats", stats)

    # ------------------------------------------------------------------
    # Ratings CRUD
    # ------------------------------------------------------------------
    async def get_all_ratings(self) -> dict[str, Any]:
        return await self._read("ratings")

    async def save_rating(self, ticket_id: str, data: dict) -> None:
        ratings = await self._read("ratings")
        ratings[ticket_id] = data
        await self._write("ratings", ratings)
