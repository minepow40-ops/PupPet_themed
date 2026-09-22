"""
utils/api_server.py
Lightweight aiohttp REST API — serves dashboard data and accepts config writes.
Runs inside the same event loop as the bot.

Endpoints:
  GET  /api/data          → { tickets, stats, ratings, config }
  GET  /api/tickets       → tickets.json
  GET  /api/stats         → stats.json
  GET  /api/ratings       → ratings.json
  GET  /api/config        → config.json
  POST /api/config        → overwrite config fields (JSON body)
  GET  /api/health        → { status: "ok", bot_ready: bool }
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aiohttp import web

log = logging.getLogger("PupPet.api")

DATA_DIR = Path(__file__).parent.parent / "data"
CONFIG_PATH = DATA_DIR / "config.json"

# CORS headers so the HTML dashboard (file://) can fetch freely
CORS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _json_resp(data, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        status=status,
        content_type="application/json",
        headers=CORS,
    )


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


class DashboardAPI:
    """Wraps an aiohttp Application and wires it to the bot's DataManager."""

    def __init__(self, bot, host: str = "127.0.0.1", port: int = 5000):
        self.bot  = bot
        self.host = host
        self.port = port
        self._app    = web.Application()
        self._runner = None

        self._app.router.add_route("OPTIONS", "/{path_info:.*}", self._options)
        self._app.router.add_get("/api/health",  self._health)
        self._app.router.add_get("/api/data",    self._all_data)
        self._app.router.add_get("/api/tickets", self._tickets)
        self._app.router.add_get("/api/stats",   self._stats)
        self._app.router.add_get("/api/ratings", self._ratings)
        self._app.router.add_get("/api/config",  self._config_get)
        self._app.router.add_post("/api/config", self._config_post)

    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("Dashboard API listening on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ── Handlers ──────────────────────────────────────────────────────────
    async def _options(self, _req):
        return web.Response(headers=CORS)

    async def _health(self, _req):
        return _json_resp({"status": "ok", "bot_ready": self.bot.is_ready()})

    async def _all_data(self, _req):
        dm = self.bot.data
        return _json_resp({
            "tickets": await dm.get_tickets(),
            "stats":   await dm.get_all_stats(),
            "ratings": await dm.get_all_ratings(),
            "config":  _read_json(CONFIG_PATH),
        })

    async def _tickets(self, _req):
        return _json_resp(await self.bot.data.get_tickets())

    async def _stats(self, _req):
        return _json_resp(await self.bot.data.get_all_stats())

    async def _ratings(self, _req):
        return _json_resp(await self.bot.data.get_all_ratings())

    async def _config_get(self, _req):
        return _json_resp(_read_json(CONFIG_PATH))

    async def _config_post(self, req: web.Request):
        try:
            body = await req.json()
        except Exception:
            return _json_resp({"error": "Invalid JSON"}, 400)

        cfg = _read_json(CONFIG_PATH)
        cfg.update(body)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

        # Hot-reload the bot's Config object so changes take effect immediately
        try:
            self.bot.config.reload()
        except Exception as exc:
            log.warning("Config hot-reload failed: %s", exc)

        log.info("Config updated via dashboard API.")
        return _json_resp({"status": "saved"})
