"""
Wrapper entrypoint dla siwz-agent backend.

Używany zamiast `main:app` w uvicornie, bo `main.py` jest nadpisywany
przez zewnętrzny watchdog/hook (prawdopodobnie deployment tool) i moje
`include_router` znikają między restartami.

Ten plik:
    1. Importuje moduł `main` (wykonuje się `app = FastAPI(...)`)
    2. Mountuje router kosztorysów v2/v3
    3. Auto-discovery modułów z modules/
    4. Re-exportuje `app` jako top-level symbol

Uvicorn: `uvicorn app:app --host 0.0.0.0 --port 8001`
Systemd: ExecStart wymagany update na `app:app`.
"""

import logging

import main  # ten import wykonuje main.py — FastAPI app już istnieje
from api_kosztorys import router as kosztorys_v2_router
from api_kosztorys_v3 import router as kosztorys_v3_router
from core.module_loader import discover_and_mount_modules

log = logging.getLogger("siwz")

# ── Mount kosztorys routers (legacy) ─────────────────

# WAŻNE: v3 PRZED v2 bo ma statyczne ścieżki typu /szablony, /biblioteka
# które koliduja z `/{kosztorys_id}` z v2 (FastAPI matchuje w kolejności
# dodawania).

_v3_mounted = any(
    "szablony" in str(getattr(r, "path", ""))
    for r in main.app.routes
)
if not _v3_mounted:
    main.app.include_router(kosztorys_v3_router)
    log.info("Kosztorysy v3 router mounted (%d routes)",
             len(kosztorys_v3_router.routes))

_v2_mounted = any(
    "przelicz" in str(getattr(r, "path", ""))
    for r in main.app.routes
)
if not _v2_mounted:
    main.app.include_router(kosztorys_v2_router)
    log.info("Kosztorysy v2 router mounted (%d routes)",
             len(kosztorys_v2_router.routes))

# ── Auto-discovery modułów pluginowych ───────────────

_modules = discover_and_mount_modules(main.app)

# ── ERP Auto-Sync (background task) ─────────────────
@main.app.on_event("startup")
async def _start_erp_auto_sync():
    """Uruchom background sync z ERP co N minut."""
    try:
        from modules.erp_sync.router import _auto_sync_loop
        import asyncio
        asyncio.create_task(_auto_sync_loop())
        log.info("🔄 ERP Auto-Sync: task zarejestrowany")
    except Exception as e:
        log.warning(f"⚠️ ERP Auto-Sync: nie uruchomiony ({e})")

# Re-export
app = main.app

# ── Serwuj frontend (SPA) z backendu ────────────────
# Cloudflare Tunnel kieruje platforma.wskonsorcjum.pl → localhost:8001
# więc backend musi serwować też pliki frontendu dla ścieżek nie-API.
import os
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

_frontend_dir = Path("/home/wskpawelw/siwz-agent/frontend")
if _frontend_dir.exists() and (_frontend_dir / "index.html").exists():
    # Serwuj /assets/* jako static files
    _assets_dir = _frontend_dir / "assets"
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="static_assets")

    # Serwuj favicon i inne pliki root-level
    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        return FileResponse(str(_frontend_dir / "favicon.svg"))

    @app.get("/icons.svg", include_in_schema=False)
    async def icons():
        return FileResponse(str(_frontend_dir / "icons.svg"))

    # SPA catch-all — MUSI być na końcu (po /api/*)
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # Nie przechwytuj ścieżek API i docs
        if full_path.startswith(("api/", "docs", "redoc", "openapi")):
            from fastapi import HTTPException
            raise HTTPException(status_code=404)
        # Sprawdź czy istnieje plik statyczny
        file_path = _frontend_dir / full_path
        if full_path and file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        # Dla wszystkich innych ścieżek zwróć index.html (SPA routing)
        # no-store: index.html zawiera guard /dashboard — musi być zawsze świeży (nie hashowany)
        return FileResponse(str(_frontend_dir / "index.html"),
                            headers={"Cache-Control": "no-store, must-revalidate"})

    log.info("Frontend SPA mounted from %s", _frontend_dir)
else:
    log.warning("Frontend directory not found at %s — SPA not mounted", _frontend_dir)
