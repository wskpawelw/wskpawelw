"""
Moduł Analizator — serwuje NOWY front (jako /dashboard + /analizator) i API silnika audytu.
Wszystko za loginem platformy (JWT). Addytywne: nie rusza istniejących modułów ani Reacta.
Cofnięcie = usunięcie tego folderu + restart.
"""
import os, sys, json, time, uuid, asyncio, threading, re
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


# ---- Google Sheet z audytu (konwersja xlsx -> natywny arkusz Google) ----
# SA crewai-wsk: tworzy/aktualizuje arkusz w Shared Drive WSKonsorcjum, stały URL
# per audyt, odświeżany w miejscu gdy xlsx się zmieni. Patrz [[project_analizator_platforma]].
_SA_JSON      = "/home/wskpawelw/.config/wsk-sa/crewai-wsk.json"
_SHARED_DRIVE = "0AAFq0r_j8xmkUk9PVA"
_SHEETS_FOLDER= "Audyty_Arkusze_Google"
_AUDYT_OUT    = "/home/wskpawelw/audyt/outputs"
_SHEET_CACHE  = os.path.join(os.path.dirname(__file__), "sheets_map.json")
_SHEET_GRANT  = "pawel.werema@wskonsorcjum.pl"   # gwarancja otwarcia (writer)
_XLSX_MIME    = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SHEET_MIME   = "application/vnd.google-apps.spreadsheet"
_drive_state  = {"svc": None, "folder": None}
_sheet_lock   = threading.Lock()


def _drive():
    if _drive_state["svc"] is None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_file(
            _SA_JSON, scopes=["https://www.googleapis.com/auth/drive"])
        _drive_state["svc"] = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_state["svc"]


def _folder_id():
    if _drive_state["folder"]:
        return _drive_state["folder"]
    drv = _drive()
    q = ("name='%s' and mimeType='application/vnd.google-apps.folder' and trashed=false"
         % _SHEETS_FOLDER)
    r = drv.files().list(q=q, corpora="drive", driveId=_SHARED_DRIVE,
                         includeItemsFromAllDrives=True, supportsAllDrives=True,
                         fields="files(id)").execute()
    fs = r.get("files", [])
    fid = fs[0]["id"] if fs else drv.files().create(
        body={"name": _SHEETS_FOLDER, "mimeType": "application/vnd.google-apps.folder",
              "parents": [_SHARED_DRIVE]}, supportsAllDrives=True, fields="id").execute()["id"]
    _drive_state["folder"] = fid
    return fid


def _load_cache():
    try:
        return json.load(open(_SHEET_CACHE, encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(c):
    try:
        json.dump(c, open(_SHEET_CACHE, "w", encoding="utf-8"))
    except Exception as e:
        print("sheet cache save err", e, file=sys.stderr)


def _find_sheet_by_name(aid):
    drv = _drive()
    q = ("name='%s' and mimeType='%s' and '%s' in parents and trashed=false"
         % (aid, _SHEET_MIME, _folder_id()))
    r = drv.files().list(q=q, corpora="drive", driveId=_SHARED_DRIVE,
                         includeItemsFromAllDrives=True, supportsAllDrives=True,
                         fields="files(id)").execute()
    fs = r.get("files", [])
    return fs[0]["id"] if fs else None


def _grant(file_id):
    try:
        _drive().permissions().create(
            fileId=file_id, supportsAllDrives=True, sendNotificationEmail=False,
            body={"type": "user", "role": "writer", "emailAddress": _SHEET_GRANT}).execute()
    except Exception:
        pass  # już ma dostęp / błąd nieblokujący


def _ensure_sheet(aid):
    path = os.path.join(_AUDYT_OUT, aid + ".xlsx")
    if not os.path.exists(path):
        return None
    from googleapiclient.http import MediaFileUpload
    mtime = int(os.path.getmtime(path))
    with _sheet_lock:
        cache = _load_cache()
        ent = cache.get(aid) or {}
        fid = ent.get("id") or _find_sheet_by_name(aid)
        drv = _drive()
        if not fid:
            media = MediaFileUpload(path, mimetype=_XLSX_MIME, resumable=False)
            f = drv.files().create(
                body={"name": aid, "mimeType": _SHEET_MIME, "parents": [_folder_id()]},
                media_body=media, supportsAllDrives=True, fields="id").execute()
            fid = f["id"]; _grant(fid)
        elif ent.get("mtime") != mtime:
            # xlsx się zmienił -> odśwież treść w miejscu (stały URL)
            media = MediaFileUpload(path, mimetype=_XLSX_MIME, resumable=False)
            drv.files().update(fileId=fid, media_body=media, supportsAllDrives=True,
                               fields="id").execute()
            _grant(fid)
        cache[aid] = {"id": fid, "mtime": mtime}
        _save_cache(cache)
    return "https://docs.google.com/spreadsheets/d/%s/edit" % fid


@router.get("/api/analizator/sheet/{aid}")
def api_sheet(aid: str, user=Depends(get_current_user)):
    if not re.match(r"^AUDYT_[A-Za-z0-9_]+$", aid):
        return JSONResponse({"error": "Zła nazwa audytu."}, status_code=400)
    try:
        url = _ensure_sheet(aid)
    except Exception as e:
        print("sheet err", aid, e, file=sys.stderr)
        return JSONResponse({"error": "Nie udało się otworzyć arkusza Google."}, status_code=502)
    if not url:
        return JSONResponse({"error": "Brak pliku xlsx dla tego audytu."}, status_code=404)
    return JSONResponse({"url": url})
