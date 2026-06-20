"""
MYTHOS — web layer: FastAPI + websocket state push + static dashboard.
"""

import asyncio
import json
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .archive import archive_day
from .state import build_state, overlay_live_prices, price_frame

log = logging.getLogger("mythos.server")


def _safe_dumps(obj):
    """Serialize with allow_nan=False so a stray NaN/Inf (one slips past _f and the
    broker can emit them) raises HERE — a caught, single skipped frame — instead of
    emitting `NaN` into the JSON, which is invalid and wedges the client's
    JSON.parse, silently FREEZING the dashboard (the 2026-06-15 failure mode).
    Returns None on failure so the caller simply skips that one push."""
    try:
        return json.dumps(obj, allow_nan=False)
    except (ValueError, TypeError) as e:
        log.debug("state serialize skipped (non-finite?): %s", e)
        return None


def create_server(app_core) -> FastAPI:
    api = FastAPI(title="MYTHOS", docs_url=None, redoc_url=None)

    @api.middleware("http")
    async def no_cache(request, call_next):
        # the dashboard must NEVER be stale — a cached app.js once hid a
        # finished feature from the user mid-session
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp

    @api.get("/")
    async def index():
        import os
        return FileResponse(os.path.join(config.STATIC_DIR, "index.html"))

    @api.get("/api/state")
    async def state():
        return JSONResponse(build_state(app_core))

    @api.post("/api/archive")
    async def archive():
        path = archive_day(app_core.trader)
        return {"ok": bool(path), "path": path or "no trades to archive"}

    @api.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        log.info("dashboard connected: %s", sock.client)
        # SINGLE send-loop at the FAST price cadence. Every tick it pushes a tiny
        # price-only frame (lock-free freeze_core, microseconds), so PRICES NEVER
        # WAIT behind the heavy state build (Requirement §3 — the user's #1 ask).
        # The heavy build runs in a BACKGROUND thread-task that is never awaited
        # inline; when it finishes, its full frame goes out on the next tick. All
        # sends happen in THIS one coroutine, so there is no concurrent-send hazard
        # and no lock — the price path and the full path can't tear each other.
        ticks_per_full = max(1, round(config.UI_PUSH_MS / config.PRICE_PUSH_MS))
        build_task = None
        tick = 0
        try:
            while True:
                # 1) FAST price frame — every tick, independent of the build.
                pf = _safe_dumps(price_frame(app_core))
                if pf is not None:
                    await sock.send_text(pf)

                # 2) Kick a heavy build periodically if none is in flight (a slow
                #    build simply means the next kick waits — no pile-up).
                if build_task is None and tick % ticks_per_full == 0:
                    build_task = asyncio.create_task(
                        asyncio.to_thread(build_state, app_core))

                # 3) Harvest a finished build and push the full tree. Cache the
                #    last-good tree so a rare transient never drops the full frame.
                if build_task is not None and build_task.done():
                    try:
                        state = build_task.result()
                        state["kind"] = "full"
                        app_core._last_good_state = state
                    except Exception as e:
                        log.debug("build_state failed; serving last-good: %s", e)
                        state = getattr(app_core, "_last_good_state", None)
                    build_task = None
                    if state is not None:
                        overlay_live_prices(app_core, state)
                        fd = _safe_dumps(state)
                        if fd is not None:
                            await sock.send_text(fd)

                tick += 1
                await asyncio.sleep(config.PRICE_PUSH_MS / 1000.0)
        except (WebSocketDisconnect, ConnectionError):
            log.info("dashboard disconnected")
        except Exception as e:
            log.debug("ws push failed: %s", e)
        finally:
            if build_task is not None and not build_task.done():
                build_task.cancel()

    api.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
    api.mount("/assets", StaticFiles(directory=config.ASSETS_DIR), name="assets")
    return api


def run_server(app_core):
    import uvicorn
    api = create_server(app_core)
    uvicorn.run(api, host=config.HOST, port=config.PORT, log_level="warning")
