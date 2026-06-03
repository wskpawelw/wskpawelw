"""
Moduł Analizator — serwuje NOWY front (jako /dashboard + /analizator) i API silnika audytu.
Wszystko za loginem platformy (JWT). Addytywne: nie rusza istniejących modułów ani Reacta.
Cofnięcie = usunięcie tego folderu + restart.
"""
import os, sys, json, time, uuid, asyncio, threading
from fastapi import APIRouter, Depends, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jose import jwt, JWTError
from core.security import get_current_user, JWT_SECRET, JWT_ALGORITHM

# Silnik audytu (parser xlsx + joby demo/real) z ~/audyt
sys.path.insert(0, "/home/wskpawelw/audyt/app")
sys.path.insert(0, "/home/wskpawelw/audyt/scripts")
import server as ENG  # list_audits, full_audit, run_demo, run_real, JOBS, jset, LOCK

router = APIRouter()
_HTML = os.path.join(os.path.dirname(__file__), "app.html")


def _verify_token(token: str):
    if not token:
        raise HTTPException(status_code=401, detail="Brak autoryzacji")
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Nieważny token")


# ---- STRONA (przejmuje /dashboard; alias /analizator) — sama jest publicznym shellem,
#      dane lecą wyłącznie przez API za tokenem ----
@router.get("/dashboard", include_in_schema=False)
def page_dashboard():
    return HTMLResponse(open(_HTML, encoding="utf-8").read())

@router.get("/analizator", include_in_schema=False)
def page_analizator():
    return HTMLResponse(open(_HTML, encoding="utf-8").read())


# ---- API (za loginem) ----
@router.get("/api/analizator/audits")
def api_audits(user=Depends(get_current_user)):
    return JSONResponse(ENG.list_audits())

@router.get("/api/analizator/audit/{aid}")
def api_audit(aid: str, user=Depends(get_current_user)):
    d = ENG.full_audit(aid)
    if not d:
        return JSONResponse({"error": "nie znaleziono"}, status_code=404)
    return JSONResponse(d)

@router.post("/api/analizator/analyze")
async def api_analyze(request: Request, user=Depends(get_current_user)):
    body = await request.json()
    url = (body.get("url") or "").strip()
    mode = body.get("mode", "demo")
    if not url:
        return JSONResponse({"error": "Podaj link do folderu Google Drive."}, status_code=400)
    jid = uuid.uuid4().hex[:12]
    ENG.jset(jid, pct=0, stage="Inicjalizacja", started=int(time.time()), mode=mode, url=url)
    tgt = ENG.run_real if mode == "real" else ENG.run_demo
    threading.Thread(target=tgt, args=(jid, url), daemon=True).start()
    return JSONResponse({"job_id": jid, "mode": mode})

# SSE — EventSource nie umie nagłówków, więc token w query (walidowany ręcznie)
@router.get("/api/analizator/progress/{jid}")
async def api_progress(jid: str, token: str = Query(None)):
    _verify_token(token)

    async def gen():
        last = -1
        last_len = 0
        while True:
            with ENG.LOCK:
                j = ENG.JOBS.get(jid)
                snap = dict(j) if j else None
            if snap is None:
                yield "event: error\ndata: {}\n\n"
                return
            if snap["pct"] != last or len(snap["log"]) != last_len:
                last = snap["pct"]
                last_len = len(snap["log"])
                yield "data: " + json.dumps(snap, ensure_ascii=False) + "\n\n"
            if snap.get("done"):
                yield "event: done\ndata: " + json.dumps(snap, ensure_ascii=False) + "\n\n"
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
