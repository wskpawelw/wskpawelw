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
_NOCACHE = {"Cache-Control": "no-store, must-revalidate"}

@router.get("/dashboard", include_in_schema=False)
def page_dashboard():
    return HTMLResponse(open(_HTML, encoding="utf-8").read(), headers=_NOCACHE)

@router.get("/analizator", include_in_schema=False)
def page_analizator():
    return HTMLResponse(open(_HTML, encoding="utf-8").read(), headers=_NOCACHE)


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
        return JSONResponse({"error": "Nie udało się otworzyć arkusza Google."}, status_code=200)
    if not url:
        return JSONResponse({"error": "Brak pliku xlsx dla tego audytu."}, status_code=404)
    return JSONResponse({"url": url})


# ---- Otwieranie dokumentu źródłowego z Google Drive (klikalne dokumenty w "Co przeanalizowane") ----
# Mapowanie audyt -> źródłowy folder Drive (audit nie zapisuje go sam). Format:
#   { "AUDYT_X": {"folder_id": "<id folderu>", "drive_id": "<id Shared Drive lub pusty>"} }
_SOURCES = os.path.join(os.path.dirname(__file__), "audit_sources.json")
_files_cache = {}   # aid -> [{"id","name"}...] (rekursywny indeks plików folderu)


def _load_sources():
    try:
        return json.load(open(_SOURCES, encoding="utf-8"))
    except Exception:
        return {}


def _walk(folder_id, drive_id, depth=4, acc=None):
    if acc is None: acc=[]
    if depth < 0 or len(acc) > 600: return acc
    drv=_drive()
    kw=dict(q="'%s' in parents and trashed=false" % folder_id, fields="files(id,name,mimeType)",
            pageSize=200, supportsAllDrives=True, includeItemsFromAllDrives=True)
    if drive_id: kw.update(corpora="drive", driveId=drive_id)
    else: kw.update(corpora="allDrives")
    page=None
    while True:
        if page: kw["pageToken"]=page
        r=drv.files().list(**kw).execute()
        for f in r.get("files", []):
            if f.get("mimeType")=="application/vnd.google-apps.folder":
                _walk(f["id"], drive_id, depth-1, acc)
            else:
                acc.append({"id":f["id"],"name":f["name"]})
        page=r.get("nextPageToken")
        if not page: break
    return acc


_norm_re=re.compile(r"[^a-z0-9ąćęłńóśźż]+")
def _tokens(s):
    s=(s or "").lower().replace("ł","l")
    return set(t for t in _norm_re.split(s) if len(t)>=3)


def _resolve_doc(aid, query):
    src=_load_sources().get(aid)
    if not src or not src.get("folder_id"):
        return {"error": "no_source"}
    fid=src["folder_id"]; did=src.get("drive_id","")
    files=_files_cache.get(aid)
    if files is None:
        files=_walk(fid, did)
        _files_cache[aid]=files
    folder_url="https://drive.google.com/drive/folders/%s" % fid
    if not files:
        return {"url": folder_url, "match": "folder"}
    qt=_tokens(query)
    best=None; best_score=0
    for f in files:
        ft=_tokens(f["name"])
        if not ft: continue
        inter=len(qt & ft)
        score=inter/max(1, min(len(qt), len(ft)))
        if inter>0 and score>best_score:
            best_score=score; best=f
    if best and best_score>=0.34:
        return {"url": "https://drive.google.com/file/d/%s/view" % best["id"],
                "match": "file", "name": best["name"]}
    return {"url": folder_url, "match": "folder"}


@router.get("/api/analizator/docfile/{aid}")
def api_docfile(aid: str, q: str = Query(""), user=Depends(get_current_user)):
    if not re.match(r"^AUDYT_[A-Za-z0-9_]+$", aid):
        return JSONResponse({"error": "Zła nazwa audytu."}, status_code=400)
    try:
        res=_resolve_doc(aid, q)
    except Exception as e:
        print("docfile err", aid, e, file=sys.stderr)
        return JSONResponse({"error": "Błąd odczytu folderu Drive."}, status_code=200)
    if res.get("error")=="no_source":
        return JSONResponse({"error": "no_source"}, status_code=404)
    return JSONResponse(res)


# ---- Pismo "Wniosek o wyjaśnienie treści SWZ" -> Google Docs (z pytań audytu) ----
_PISMO_CACHE = os.path.join(os.path.dirname(__file__), "pisma_map.json")
_DOC_MIME = "application/vnd.google-apps.document"


def _meta_get(metryka, *frags):
    for m in metryka or []:
        if any(f in (m.get("pole") or "").lower() for f in frags):
            return m.get("wartosc") or ""
    return ""


def _build_pismo_html(aid, d):
    import html as _h
    meta = (d.get("coverage") or {}).get("metryka", [])
    zam = _meta_get(meta, "zamawiaj")
    nazwa = _meta_get(meta, "nazwa zadania", "nazwa post", "przedmiot") or (d.get("meta") or {}).get("project", "")
    nr = _meta_get(meta, "nr post", "oznaczenie spraw", "numer post", "znak spraw") or (d.get("meta") or {}).get("bzp", "")
    qs = d.get("questions") or []
    items = "".join("<li style=\"margin-bottom:8px\">%s</li>" % _h.escape(str(q)) for q in qs)
    e = lambda x: _h.escape(str(x or ""))
    return (
        "<html><head><meta charset=\"utf-8\"></head>"
        "<body style=\"font-family:'Times New Roman',serif;font-size:12pt;line-height:1.5\">"
        "<p>…………………………, dnia ……………………</p>"
        "<p><b>WSK Konsorcjum</b><br>[adres Wykonawcy]<br>NIP: …………………………</p>"
        "<p style=\"text-align:right;margin-top:24px\"><b>Do:</b><br>" + (e(zam) or "[Zamawiający]") + "</p>"
        "<p style=\"margin-top:24px\"><b>Dotyczy:</b> postępowania pn. „" + e(nazwa) + "”<br>"
        "<b>Nr postępowania:</b> " + (e(nr) or "—") + "</p>"
        "<h2 style=\"text-align:center;margin-top:24px\">WNIOSEK O WYJAŚNIENIE TREŚCI SWZ</h2>"
        "<p>Działając w imieniu Wykonawcy, na podstawie art. 135 ust. 1 ustawy z dnia 11 września 2019 r. – "
        "Prawo zamówień publicznych, zwracamy się z wnioskiem o wyjaśnienie treści Specyfikacji Warunków "
        "Zamówienia (SWZ) w następującym zakresie:</p>"
        "<ol>" + items + "</ol>"
        "<p style=\"margin-top:16px\">Mając na uwadze powyższe, wnosimy o udzielenie wyjaśnień, a w razie "
        "potrzeby o odpowiednie przedłużenie terminu składania ofert.</p>"
        "<p style=\"margin-top:40px\">Z poważaniem,</p>"
        "<p style=\"margin-top:32px\">……………………………………………<br>(podpis osoby upoważnionej)</p>"
        "</body></html>"
    )


def _ensure_pismo(aid):
    path = os.path.join(_AUDYT_OUT, aid + ".xlsx")
    if not os.path.exists(path):
        return None
    d = ENG.full_audit(aid)
    if not d or not (d.get("questions")):
        return {"error": "no_questions"}
    from googleapiclient.http import MediaInMemoryUpload
    html_doc = _build_pismo_html(aid, d)
    mtime = int(os.path.getmtime(path))
    with _sheet_lock:
        try:
            cache = json.load(open(_PISMO_CACHE, encoding="utf-8"))
        except Exception:
            cache = {}
        ent = cache.get(aid) or {}
        fid = ent.get("id")
        drv = _drive()
        media = MediaInMemoryUpload(html_doc.encode("utf-8"), mimetype="text/html", resumable=False)
        if not fid:
            f = drv.files().create(
                body={"name": aid + "_pismo", "mimeType": _DOC_MIME, "parents": [_folder_id()]},
                media_body=media, supportsAllDrives=True, fields="id").execute()
            fid = f["id"]; _grant(fid)
        elif ent.get("mtime") != mtime:
            drv.files().update(fileId=fid, media_body=media, supportsAllDrives=True, fields="id").execute()
            _grant(fid)
        cache[aid] = {"id": fid, "mtime": mtime}
        try:
            json.dump(cache, open(_PISMO_CACHE, "w", encoding="utf-8"))
        except Exception as e:
            print("pismo cache err", e, file=sys.stderr)
    return {"url": "https://docs.google.com/document/d/%s/edit" % fid}


@router.get("/api/analizator/pismo/{aid}")
def api_pismo(aid: str, user=Depends(get_current_user)):
    if not re.match(r"^AUDYT_[A-Za-z0-9_]+$", aid):
        return JSONResponse({"error": "Zła nazwa audytu."}, status_code=400)
    try:
        res = _ensure_pismo(aid)
    except Exception as e:
        print("pismo err", aid, e, file=sys.stderr)
        return JSONResponse({"error": "Nie udało się utworzyć pisma."}, status_code=200)
    if not res:
        return JSONResponse({"error": "Brak pliku audytu."}, status_code=404)
    if res.get("error") == "no_questions":
        return JSONResponse({"error": "Ten audyt nie ma gotowych pytań do zamawiającego."}, status_code=404)
    return JSONResponse(res)


_SA_EMAIL = "crewai-wsk@erp-bud2.iam.gserviceaccount.com"


@router.post("/api/analizator/source/{aid}")
async def api_set_source(aid: str, request: Request, user=Depends(get_current_user)):
    if not re.match(r"^AUDYT_[A-Za-z0-9_]+$", aid):
        return JSONResponse({"error": "Zła nazwa audytu."}, status_code=400)
    body = await request.json()
    url = (body.get("url") or "").strip()
    m = (re.search(r"/folders/([A-Za-z0-9_-]{20,})", url)
         or re.search(r"[?&]id=([A-Za-z0-9_-]{20,})", url)
         or re.match(r"^([A-Za-z0-9_-]{20,})$", url))
    if not m:
        return JSONResponse({"error": "Nie rozpoznałem ID folderu w tym linku."}, status_code=400)
    fid = m.group(1)
    try:
        meta = _drive().files().get(fileId=fid, fields="id,name,mimeType,driveId",
                                    supportsAllDrives=True).execute()
    except Exception:
        return JSONResponse({"ok": False, "error": "Folder niedostępny dla konta serwisowego. Udostępnij ten folder dla %s (rola Czytelnik) i spróbuj ponownie." % _SA_EMAIL}, status_code=200)
    if meta.get("mimeType") != "application/vnd.google-apps.folder":
        return JSONResponse({"error": "Podany link to nie jest folder Google Drive."}, status_code=400)
    src = _load_sources()
    src[aid] = {"folder_id": fid, "drive_id": meta.get("driveId", "")}
    try:
        json.dump(src, open(_SOURCES, "w", encoding="utf-8"))
    except Exception as e:
        print("sources save err", e, file=sys.stderr)
        return JSONResponse({"error": "Nie udało się zapisać konfiguracji."}, status_code=200)
    _files_cache.pop(aid, None)
    return JSONResponse({"ok": True, "name": meta.get("name", "")})
