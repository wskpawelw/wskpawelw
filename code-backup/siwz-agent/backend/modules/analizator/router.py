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
    audits = ENG.list_audits()
    zrodla = _load_sources()
    for a in audits:
        a["ma_folder"] = bool((zrodla.get(a["id"]) or {}).get("folder_id"))
    return JSONResponse(audits)


# ---- Powiązania audyt ↔ postępowanie (projekt z /api/przetargi) ----
# Ręczne powiązania w audit_projekty.json (override), reszta auto-match po
# nazwach (stemy 6-znakowe tokenów, bez słów typowych dla budowlanki).
_LINKS_FILE = os.path.join(os.path.dirname(__file__), "audit_projekty.json")
_LINKS_LOCK = threading.Lock()

_STOPSTEMY = {"budowa", "budowy", "budowl", "rozbud", "przebu", "remont",
              "roboty", "budynk", "prace", "wykona", "zadani", "inwest"}


def _load_links() -> dict:
    try:
        with open(_LINKS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_links(links: dict):
    with open(_LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(links, f, ensure_ascii=False, indent=1)


def _stemy(nazwa: str) -> set:
    """Tokeny nazwy → 6-znakowe stemy bez polskich znaków i słów-wytrychów."""
    import unicodedata
    s = unicodedata.normalize("NFKD", (nazwa or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    stemy = {t[:6] for t in s.split() if len(t) >= 4}
    return stemy - _STOPSTEMY


def _data_norm(s: str) -> str:
    """Wyciąga datę DD.MM.YYYY z dowolnego napisu (audyt: '07.07.2026 godz. 09:30',
    portal: '07.07.2026 09:30', '2026-07-07'). Zwraca 'dd.mm.yyyy' albo ''."""
    s = str(s or "")
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return "%02d.%02d.%s" % (int(m.group(1)), int(m.group(2)), m.group(3))
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)  # ISO
    if m:
        return "%02d.%02d.%s" % (int(m.group(3)), int(m.group(2)), m.group(1))
    return ""


def _auto_match(audits: list, projekty: list) -> dict:
    """Auto-dopasowanie audytów do projektów: ≥2 wspólne stemy, jednoznaczny zwycięzca.

    Remis liczby stemów (np. dwie termomodernizacje w Kołobrzegu) rozstrzyga
    zgodność terminu składania audyt↔postępowanie — jeśli wskazuje dokładnie
    jednego kandydata. Bez rozstrzygnięcia audyt zostaje niepowiązany (jak dotąd).
    """
    wynik = {}
    for a in audits:
        sa = _stemy(a.get("project", ""))
        kandydaci = []
        for p in projekty:
            wspolne = len(sa & _stemy(p["nazwa"]))
            if wspolne >= 2:
                kandydaci.append((wspolne, p["id"], _data_norm(p.get("termin"))))
        kandydaci.sort(key=lambda k: k[0], reverse=True)
        if not kandydaci:
            continue
        top = kandydaci[0][0]
        czolo = [k for k in kandydaci if k[0] == top]
        if len(czolo) == 1:
            wynik[a["id"]] = czolo[0][1]
            continue
        # remis na liczbie stemów → rozstrzygnij po terminie składania
        adt = _data_norm(a.get("termin"))
        if adt:
            zgodni = [k for k in czolo if k[2] and k[2] == adt]
            if len(zgodni) == 1:
                wynik[a["id"]] = zgodni[0][1]
    return wynik


@router.get("/api/analizator/audit-links")
async def api_audit_links(user=Depends(get_current_user)):
    """Mapa audit_id → projekt_id: ręczne powiązania + auto-match po nazwach."""
    from database import async_session
    from models import Projekt
    from sqlalchemy import select as sa_select

    async with async_session() as db:
        rows = (await db.execute(sa_select(Projekt.id, Projekt.nazwa, Projekt.termin_skladania))).all()
    projekty = [{"id": r[0], "nazwa": r[1] or "", "termin": r[2]} for r in rows]

    audits = ENG.list_audits()
    auto = _auto_match(audits, projekty)
    manual = _load_links()
    linki = dict(auto)
    for aid, pid in manual.items():
        if pid is None:
            linki.pop(aid, None)      # ręcznie odpięte — nie pokazuj auto
        else:
            linki[aid] = pid
    return JSONResponse({"links": linki, "manual": manual, "auto": auto})


# ---- Pobieranie nowych dokumentów z portalu przetargowego do folderu Drive ----
# Domyka pętlę monitoringu: monitor wykrył nowe pliki (diff_json ma ich URL-e),
# ten endpoint ściąga je z portalu i wrzuca do źródłowego folderu Drive audytu
# (audit_sources.json) — potem "Aktualizuj audyt" je dogrywa.

def _effective_links(audits, projekty):
    """Mapa audit_id → projekt_id (ręczne nadpisuje auto)."""
    linki = _auto_match(audits, projekty)
    for aid, pid in _load_links().items():
        if pid is None:
            linki.pop(aid, None)
        else:
            linki[aid] = pid
    return linki


def _pobierz_do_drive(urls: dict, folder_id: str, drive_id: str) -> dict:
    """Ściągnij pliki (nazwa→url) i wgraj do folderu Drive. Zwraca raport."""
    import httpx
    from io import BytesIO
    from googleapiclient.http import MediaIoBaseUpload

    drv = _drive()
    raport = {"pobrane": [], "pominiete": [], "bledy": []}
    ua = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126 Safari/537.36"}
    for nazwa, url in urls.items():
        try:
            # idempotencja: nie dubluj pliku o tej nazwie w folderze
            q = "name='%s' and '%s' in parents and trashed=false" % (
                nazwa.replace("'", "\\'"), folder_id)
            kw = dict(q=q, fields="files(id)", pageSize=1,
                      supportsAllDrives=True, includeItemsFromAllDrives=True)
            if drive_id:
                kw.update(corpora="drive", driveId=drive_id)
            else:
                kw.update(corpora="allDrives")
            if drv.files().list(**kw).execute().get("files"):
                raport["pominiete"].append(nazwa + " (już jest w folderze)")
                continue

            r = httpx.get(url, headers=ua, timeout=90, follow_redirects=True)
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200 or "text/html" in ctype:
                raport["bledy"].append(
                    nazwa + " (portal nie dał pliku bezpośrednio — pobierz ręcznie)")
                continue

            media = MediaIoBaseUpload(BytesIO(r.content),
                                      mimetype=ctype.split(";")[0] or
                                      "application/octet-stream", resumable=False)
            drv.files().create(
                body={"name": nazwa, "parents": [folder_id]},
                media_body=media, supportsAllDrives=True, fields="id").execute()
            raport["pobrane"].append(nazwa)
        except Exception as e:
            raport["bledy"].append(f"{nazwa} ({str(e)[:80]})")
    return raport


@router.post("/api/analizator/pobierz-nowe")
async def api_pobierz_nowe(request: Request, user=Depends(get_current_user)):
    """Pobierz nowe dokumenty wykryte przez monitor do folderu Drive audytu.

    Body: {projekt_id, zmiana_id}
    """
    body = await request.json()
    projekt_id = int(body.get("projekt_id") or 0)
    zmiana_id = int(body.get("zmiana_id") or 0)
    if not projekt_id or not zmiana_id:
        return JSONResponse({"error": "projekt_id i zmiana_id wymagane"}, status_code=400)

    from database import async_session
    from models import MonitorZmiana, Projekt
    from sqlalchemy import select as sa_select

    async with async_session() as db:
        zmiana = (await db.execute(
            sa_select(MonitorZmiana).where(
                MonitorZmiana.id == zmiana_id,
                MonitorZmiana.projekt_id == projekt_id)
        )).scalar_one_or_none()
        rows = (await db.execute(sa_select(Projekt.id, Projekt.nazwa, Projekt.termin_skladania))).all()
    if not zmiana:
        return JSONResponse({"error": "Nie znaleziono zmiany"}, status_code=404)

    try:
        diff = json.loads(zmiana.diff_json or "{}")
    except Exception:
        diff = {}
    urls = diff.get("dodane_dokumenty_urls") or {}
    if not urls:
        return JSONResponse({"error": "Ten wpis nie ma bezpośrednich linków do plików "
                                      "(starszy format lub portal bez linków) — pobierz ręcznie z portalu."},
                            status_code=400)

    # znajdź powiązany audyt (najnowszy, jeśli kilka wersji) i jego folder Drive
    projekty = [{"id": r[0], "nazwa": r[1] or "", "termin": r[2]} for r in rows]
    audits = ENG.list_audits()
    linki = _effective_links(audits, projekty)
    moje = [a for a in audits if linki.get(a["id"]) == projekt_id]
    if not moje:
        return JSONResponse({"error": "no_audit",
                             "hint": "Brak powiązanego audytu — najpierw zrób audyt albo powiąż istniejący."},
                            status_code=400)
    aid = max(moje, key=lambda a: a.get("mtime", 0))["id"]
    src = _load_sources().get(aid) or {}
    if not src.get("folder_id"):
        return JSONResponse({"error": "no_source", "audit_id": aid,
                             "hint": "Audyt nie ma podpiętego folderu Drive — wejdź w audyt i podepnij folder."},
                            status_code=400)

    # dostrojenie 2026-06-12: anty-dubel — drugi klik w trakcie pobierania
    # leciał równolegle i tworzył kopie plików (oba requesty widziały pusty folder)
    klucz_locka = f"pobierz:{aid}"
    if klucz_locka in _pobierania_w_toku:
        return JSONResponse({"error": "in_progress",
                             "hint": "Pobieranie już trwa — poczekaj, aż skończy."},
                            status_code=409)
    _pobierania_w_toku.add(klucz_locka)
    try:
        raport = await asyncio.to_thread(
            _pobierz_do_drive, urls, src["folder_id"], src.get("drive_id", ""))
    finally:
        _pobierania_w_toku.discard(klucz_locka)
    raport["ok"] = True
    raport["audit_id"] = aid
    raport["folder_url"] = "https://drive.google.com/drive/folders/%s" % src["folder_id"]
    # świeżo wgrane pliki mają być widoczne w indeksie klikalnych dokumentów
    _files_cache.pop(aid, None); _files_tree_cache.pop(aid, None)
    # wpis do historii zmian: widać CO i KIEDY pobrano (2026-06-12)
    pobrane = raport.get("pobrane") or []
    if pobrane:
        from models import MonitorZmiana as _MZ
        opis = "📥 Pobrano do Drive (ręcznie): " + " | ".join(pobrane[:10])
        if len(pobrane) > 10:
            opis += " (+%d więcej)" % (len(pobrane) - 10)
        async with async_session() as db2:
            db2.add(_MZ(projekt_id=projekt_id, opis_zmian=opis,
                        diff_json="{}", powiadomiono=True))
            await db2.commit()
    return JSONResponse(raport)


# ---- Paczka ofertowa: wypełnij załączniki SWZ danymi firm z kartoteki ----
@router.post("/api/analizator/paczka")
async def api_paczka(request: Request, user=Depends(get_current_user)):
    """Start jobu paczki ofertowej dla projektu. Body: {projekt_id}"""
    body = await request.json()
    pid = int(body.get("projekt_id") or 0)
    if not pid:
        return JSONResponse({"error": "projekt_id wymagane"}, status_code=400)

    from database import async_session
    from models import Projekt
    from sqlalchemy import text as sa_text, select as sa_select

    async with async_session() as db:
        projekt = (await db.execute(sa_select(Projekt).where(Projekt.id == pid))).scalar_one_or_none()
        if not projekt:
            return JSONResponse({"error": "Projekt nie istnieje"}, status_code=404)
        konf = (await db.execute(sa_text(
            "SELECT tryb, sklad_json, notatki FROM oferta_konfiguracje WHERE projekt_id=:pid"),
            {"pid": pid})).first()
        if not konf:
            return JSONResponse({"error": "brak_konfiguracji",
                                 "hint": "Najpierw zaznacz „Kim startujemy' i zapisz."}, status_code=400)
        sklad = json.loads(konf.sklad_json or "[]")
        firmy = []
        for s in sklad:
            f = (await db.execute(sa_text(
                "SELECT * FROM firmy_oferentow WHERE id=:id"), {"id": int(s["firma_id"])})).first()
            if f:
                fd = dict(f._mapping)
                for k in ("polisa_json", "referencje_json", "kadra_json"):
                    try:
                        fd[k.replace("_json", "")] = json.loads(fd.pop(k) or "null")
                    except Exception:
                        fd[k.replace("_json", "")] = None
                fd.pop("created_at", None); fd.pop("updated_at", None)
                fd["rola_w_ofercie"] = s.get("rola", "oferent")
                firmy.append(fd)
        rows = (await db.execute(sa_select(Projekt.id, Projekt.nazwa, Projekt.termin_skladania))).all()
        projekty = [{"id": r[0], "nazwa": r[1] or "", "termin": r[2]} for r in rows]

    braki = [f["nazwa"] for f in firmy if not (f.get("nip") and f.get("adres"))]
    if braki:
        return JSONResponse({"error": "braki_kartoteki",
                             "hint": "Uzupełnij NIP i adres w kartotece: " + ", ".join(braki)},
                            status_code=400)

    # folder źródłowy SWZ = folder powiązanego audytu
    aud_linki = _effective_links(ENG.list_audits(), projekty)
    moje = [a for a in ENG.list_audits() if aud_linki.get(a["id"]) == pid]
    if not moje:
        return JSONResponse({"error": "no_audit",
                             "hint": "Najpierw zrób audyt (potrzebny folder z SWZ i załącznikami)."},
                            status_code=400)
    aid = max(moje, key=lambda a: a.get("mtime", 0))["id"]
    src = _load_sources().get(aid) or {}
    if not src.get("folder_id"):
        return JSONResponse({"error": "no_source", "audit_id": aid,
                             "hint": "Audyt nie ma podpiętego folderu Drive."}, status_code=400)

    wynik_dir = f"/home/wskpawelw/audyt/oferty/PROJEKT_{pid}"
    kontekst = json.dumps({
        "postepowanie": {"id": pid, "nazwa": projekt.nazwa, "zamawiajacy": projekt.zamawiajacy,
                         "numer_ref": projekt.numer_ref, "numer_ogloszenia": projekt.numer_ogloszenia,
                         "termin_skladania": projekt.termin_skladania,
                         "url": projekt.url_postepowania},
        "tryb_oferty": konf.tryb, "notatki": konf.notatki or "",
        "podmioty": firmy,
    }, ensure_ascii=False, indent=1)

    prompt = (
        "TRYB PACZKA OFERTOWA. Przygotuj komplet wypełnionych dokumentów oferty dla postępowania.\n\n"
        f"Folder Google Drive z SWZ i załącznikami: https://drive.google.com/drive/folders/{src['folder_id']}\n"
        f"Katalog wynikowy lokalny: {wynik_dir}/ (twórz pliki TYLKO tam)\n\n"
        f"DANE OFERTY (jedyne źródło danych wykonawcy — niczego nie zmyślaj):\n{kontekst}\n\n"
        "ZADANIE:\n"
        "1. Zindeksuj folder Drive, znajdź WSZYSTKIE załączniki do wypełnienia przez wykonawcę "
        "(formularz ofertowy, oświadczenia art. 125 Pzp, wykaz robót, wykaz osób, oświadczenie "
        "o grupie kapitałowej, zobowiązanie podmiotu trzeciego, projekt pełnomocnictwa). "
        "Przeczytaj też SWZ rozdział o sposobie przygotowania oferty i wymaganych dokumentach.\n"
        "2. Dla każdego załącznika DOCX/DOC: pobierz, wypełnij python-docx danymi z DANE OFERTY "
        "(nazwa, NIP, REGON, KRS, adres, reprezentacja; przy konsorcjum: lider+partnerzy wszędzie "
        "tam gdzie wzór tego wymaga + przygotuj pełnomocnictwo dla lidera). Pola, których nie da się "
        "wypełnić z danych (np. CENA OFERTY, okres gwarancji deklarowany) zostaw z wyraźnym "
        "znacznikiem [DO UZUPEŁNIENIA: ...]. Wykaz robót wypełnij referencjami podmiotów, wykaz osób kadrą.\n"
        "3. Załączniki tylko w PDF: NIE przerabiaj — odnotuj w checkliście jako 'wypełnić ręcznie'.\n"
        "4. Stwórz CHECKLISTA_OFERTY.docx: lista dokumentów paczki (wypełnione / do uzupełnienia / "
        "ręczne), kto podpisuje (z reprezentacji), rodzaj podpisu (kwalifikowany/zaufany/osobisty wg SWZ), "
        "wadium (kwota, forma, konto, termin), termin i sposób złożenia oferty.\n"
        "5. Wgraj wszystkie wygenerowane pliki do podfolderu 'PACZKA_OFERTOWA' w folderze Drive "
        "postępowania (utwórz go).\n"
        "ZASADY: dane wykonawcy WYŁĄCZNIE z DANE OFERTY; wymagania WYŁĄCZNIE z SWZ/załączników; "
        "zero zmyślania; każdy plik nazwij jak załącznik źródłowy + sufiks _WYPELNIONY."
    )

    jid = uuid.uuid4().hex[:12]
    ENG.jset(jid, pct=0, stage="Inicjalizacja", started=int(time.time()), mode="paczka", url=str(pid))
    threading.Thread(target=ENG.run_paczka, args=(jid, prompt, wynik_dir), daemon=True).start()
    return JSONResponse({"job_id": jid, "audit_id": aid})


# ---- Raport PDF audytu (pełny obraz postępowania dla kosztorysanta) ----
@router.get("/api/analizator/pdf/{aid}")
async def api_raport_pdf(aid: str, user=Depends(get_current_user)):
    if not re.match(r"^[Aa][Uu][Dd][Yy][Tt]_[A-Za-z0-9_.-]+$", aid):
        return JSONResponse({"error": "Zła nazwa audytu."}, status_code=400)
    d = ENG.full_audit(aid)
    if not d:
        return JSONResponse({"error": "Nie znaleziono audytu."}, status_code=404)

    # dane na żywo z powiązanego postępowania + monitoringu portalu
    projekt_d, monitor_d = None, None
    try:
        from database import async_session
        from models import Projekt, MonitorPostepowania, MonitorZmiana
        from sqlalchemy import select as sa_select
        async with async_session() as db:
            rows = (await db.execute(sa_select(Projekt.id, Projekt.nazwa, Projekt.termin_skladania))).all()
            projekty = [{"id": r[0], "nazwa": r[1] or "", "termin": r[2]} for r in rows]
            pid = _effective_links(ENG.list_audits(), projekty).get(aid)
            if pid:
                p = (await db.execute(sa_select(Projekt).where(Projekt.id == pid))).scalar_one_or_none()
                if p:
                    projekt_d = {"id": p.id, "nazwa": p.nazwa,
                                 "termin_skladania": p.termin_skladania,
                                 "zamawiajacy": p.zamawiajacy}
                mon = (await db.execute(sa_select(MonitorPostepowania)
                                        .where(MonitorPostepowania.projekt_id == pid))).scalar_one_or_none()
                zmiany = (await db.execute(sa_select(MonitorZmiana)
                                           .where(MonitorZmiana.projekt_id == pid)
                                           .order_by(MonitorZmiana.wykryto_at.desc()).limit(10))).scalars().all()
                dok_portal = []
                if mon and mon.fingerprint_json:
                    try:
                        fp = json.loads(mon.fingerprint_json)
                        dok_portal = [{"nazwa": n} for n in fp.get("dokumenty", [])]
                    except Exception:
                        pass
                monitor_d = {
                    "zmiany": [{"wykryto_at": z.wykryto_at.isoformat() if z.wykryto_at else "",
                                "opis_zmian": z.opis_zmian} for z in zmiany],
                    "dokumenty_portal": dok_portal,
                }
    except Exception as e:
        print("pdf: dane projektu pominięte:", e, file=sys.stderr)

    from . import raport_pdf
    try:
        pdf = await asyncio.to_thread(raport_pdf.generuj_pdf, d, projekt_d, monitor_d)
    except Exception as e:
        print("pdf err", aid, e, file=sys.stderr)
        return JSONResponse({"error": f"Generowanie PDF nie powiodło się: {str(e)[:120]}"}, status_code=500)

    from fastapi.responses import Response
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{aid}_raport.pdf"'})


@router.post("/api/analizator/audit-links")
async def api_audit_links_set(request: Request, user=Depends(get_current_user)):
    """Zapisz/zdejmij ręczne powiązanie: {audit_id, projekt_id|null}."""
    body = await request.json()
    aid = (body.get("audit_id") or "").strip()
    pid = body.get("projekt_id", None)
    if not aid:
        return JSONResponse({"error": "audit_id wymagane"}, status_code=400)
    with _LINKS_LOCK:
        links = _load_links()
        links[aid] = int(pid) if pid is not None else None
        _save_links(links)
    return JSONResponse({"ok": True, "audit_id": aid, "projekt_id": links[aid]})

@router.get("/api/analizator/audit/{aid}")
def api_audit(aid: str, user=Depends(get_current_user)):
    d = ENG.full_audit(aid)
    if not d:
        return JSONResponse({"error": "nie znaleziono"}, status_code=404)
    return JSONResponse(d)

def _link_result_to_project(jid: str, url: str, pid: int):
    """Po udanym jobie powiąż WYNIKOWY audyt (result_id) z postępowaniem i folderem
    źródłowym — po stronie serwera, niezależnie od otwartej karty przeglądarki.
    Incydent 2026-09-11: front brał "najnowszy audyt po mtime" i powiązał Dygowo z #47,
    gdy silnik nie wyprodukował pliku. Tu wiążemy WYŁĄCZNIE result_id jobu."""
    with ENG.LOCK:
        j = dict(ENG.JOBS.get(jid) or {})
    rid = j.get("result_id")
    if not (pid and rid and j.get("ok")):
        return
    try:
        links = _load_links(); links[rid] = int(pid); _save_links(links)
        m = re.search(r"/folders/([A-Za-z0-9_-]{20,})", url) or re.search(r"[?&]id=([A-Za-z0-9_-]{20,})", url)
        if m:
            fid = m.group(1); did = ""
            try:
                meta = _drive().files().get(fileId=fid, fields="id,driveId", supportsAllDrives=True).execute()
                did = meta.get("driveId", "")
            except Exception:
                pass
            src = _load_sources(); src[rid] = {"folder_id": fid, "drive_id": did}
            json.dump(src, open(_SOURCES, "w", encoding="utf-8"))
            _files_cache.pop(rid, None); _files_tree_cache.pop(rid, None)
        ENG.jset(jid, log_add=f"OK\tAudyt {rid} powiązany z postępowaniem #{pid} i folderem źródłowym.")
    except Exception as e:
        print("link result err", e, file=sys.stderr)


def _run_then_link(tgt, jid: str, url: str, pid, hint=None):
    if hint and tgt is ENG.run_real:
        tgt(jid, url, hint)
    else:
        tgt(jid, url)
    _link_result_to_project(jid, url, pid)


@router.post("/api/analizator/analyze")
async def api_analyze(request: Request, user=Depends(get_current_user)):
    body = await request.json()
    url = (body.get("url") or "").strip()
    mode = body.get("mode", "demo")
    try:
        pid = int(body.get("projekt_id") or 0) or None
    except (TypeError, ValueError):
        pid = None
    hint = (str(body.get("hint") or "")).strip()[:2000] or None   # wskazówka operatora dla silnika
    if not url:
        return JSONResponse({"error": "Podaj link do folderu Google Drive."}, status_code=400)
    jid = uuid.uuid4().hex[:12]
    ENG.jset(jid, pct=0, stage="Inicjalizacja", started=int(time.time()), mode=mode, url=url,
             projekt_id=pid)
    tgt = ENG.run_real if mode == "real" else ENG.run_demo
    threading.Thread(target=_run_then_link, args=(tgt, jid, url, pid, hint), daemon=True).start()
    return JSONResponse({"job_id": jid, "mode": mode, "projekt_id": pid})

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
_pobierania_w_toku: set = set()   # anty-dubel pobierania do Drive
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
_files_cache = {}   # aid -> (ts, [{"id","name"}...]) — TTL 15 min (code review 2026-06-12)
_FILES_CACHE_TTL = 900


def _load_sources():
    try:
        return json.load(open(_SOURCES, encoding="utf-8"))
    except Exception:
        return {}


def _walk(folder_id, drive_id, depth=4, acc=None):
    if acc is None: acc=[]
    if depth < 0 or len(acc) > 600: return acc
    # świeży klient — _drive() to współdzielony singleton, googleapiclient
    # nie jest thread-safe a _walk bywa wołany z threadpoola FastAPI (code review 2026-06-12)
    from google.oauth2 import service_account as _sa
    from googleapiclient.discovery import build as _build
    drv=_build("drive","v3",credentials=_sa.Credentials.from_service_account_file(
        _SA_JSON, scopes=["https://www.googleapis.com/auth/drive.readonly"]),
        cache_discovery=False)
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
    import time as _t
    wpis=_files_cache.get(aid)
    files=wpis[1] if (wpis and _t.time()-wpis[0] < _FILES_CACHE_TTL) else None
    if files is None:
        files=_walk(fid, did)
        _files_cache[aid]=(_t.time(), files)
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


_files_tree_cache = {}   # aid -> (ts, tree) — podgląd folderu w widoku postępowania


@router.get("/api/analizator/files/{aid}")
def api_files_tree(aid: str, user=Depends(get_current_user)):
    """Drzewo plików folderu Drive audytu (2 poziomy) — podgląd w UI.

    dostrojenie 2026-06-12 (pomysł Pawła): sekcja "Dokumenty" w widoku
    postępowania pokazuje realny folder audytu zamiast szczątkowej tabeli
    z systemu."""
    import time as _t
    if not re.match(r"^AUDYT_[A-Za-z0-9_]+$", aid):
        return JSONResponse({"error": "Zła nazwa audytu."}, status_code=400)
    c = _files_tree_cache.get(aid)
    if c and _t.time() - c[0] < 300:
        return JSONResponse(c[1])
    src = _load_sources().get(aid)
    if not src or not src.get("folder_id"):
        return JSONResponse({"error": "no_source"}, status_code=404)
    fid, did = src["folder_id"], src.get("drive_id", "")
    try:
        from google.oauth2 import service_account as _sa
        from googleapiclient.discovery import build as _build
        _creds = _sa.Credentials.from_service_account_file(
            _SA_JSON, scopes=["https://www.googleapis.com/auth/drive.readonly"])

        def _lista(f_id):
            # ŚWIEŻY service per wywołanie — _drive() zwraca współdzielony,
            # cache'owany obiekt (googleapiclient nie jest thread-safe →
            # równoległe listowanie na nim wisiało w deadlocku)
            drv = _build("drive", "v3", credentials=_creds, cache_discovery=False)
            kw = dict(q="'%s' in parents and trashed=false" % f_id,
                      fields="files(id,name,mimeType,size,modifiedTime)", pageSize=200,
                      supportsAllDrives=True, includeItemsFromAllDrives=True)
            if did: kw.update(corpora="drive", driveId=did)
            else: kw.update(corpora="allDrives")
            return drv.files().list(**kw).execute().get("files", [])

        def _plik(f):
            return {"id": f["id"], "name": f["name"],
                    "size": int(f["size"]) if f.get("size") else None,
                    "url": "https://drive.google.com/file/d/%s/view" % f["id"]}

        korzen = _lista(fid)
        pliki_root, foldery = [], []
        # dostrojenie 2026-06-12: podfoldery listowane równolegle (wątki) —
        # sekwencyjnie widok postępowania ładował się kilkanaście sekund
        from concurrent.futures import ThreadPoolExecutor
        kat = [f for f in korzen if f["mimeType"] == "application/vnd.google-apps.folder"]
        with ThreadPoolExecutor(max_workers=12) as ex:
            sub_listy = dict(zip([f["id"] for f in kat],
                                 ex.map(lambda f: _lista(f["id"]), kat)))
        for f in sorted(korzen, key=lambda x: x["name"].lower()):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                sub = sub_listy.get(f["id"], [])
                foldery.append({
                    "name": f["name"],
                    "url": "https://drive.google.com/drive/folders/%s" % f["id"],
                    "pliki": [_plik(x) for x in sorted(sub, key=lambda x: x["name"].lower())
                              if x["mimeType"] != "application/vnd.google-apps.folder"],
                    "podfoldery": sum(1 for x in sub
                                      if x["mimeType"] == "application/vnd.google-apps.folder"),
                })
            else:
                pliki_root.append(_plik(f))
        wynik = {"folder_url": "https://drive.google.com/drive/folders/%s" % fid,
                 "pliki": pliki_root, "foldery": foldery}
        _files_tree_cache[aid] = (_t.time(), wynik)
        return JSONResponse(wynik)
    except Exception as e:
        print("files tree err", aid, e, file=sys.stderr)
        return JSONResponse({"error": "Błąd odczytu folderu Drive."}, status_code=200)


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
    # Zapisujemy folder ZAWSZE (aktualizacja audytu używa Drive użytkownika przez agenta).
    # Dostęp konta serwisowego (SA) jest potrzebny TYLKO do otwierania pojedynczych plików.
    name = ""; did = ""; sa_access = False
    try:
        meta = _drive().files().get(fileId=fid, fields="id,name,mimeType,driveId",
                                    supportsAllDrives=True).execute()
        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            return JSONResponse({"error": "Podany link to nie jest folder Google Drive."}, status_code=400)
        name = meta.get("name", ""); did = meta.get("driveId", ""); sa_access = True
    except Exception:
        pass  # SA bez dostępu — i tak zapisujemy folder (aktualizacja zadziała)
    src = _load_sources()
    src[aid] = {"folder_id": fid, "drive_id": did}
    try:
        json.dump(src, open(_SOURCES, "w", encoding="utf-8"))
    except Exception as e:
        print("sources save err", e, file=sys.stderr)
        return JSONResponse({"error": "Nie udało się zapisać konfiguracji."}, status_code=200)
    _files_cache.pop(aid, None); _files_tree_cache.pop(aid, None)
    note = "" if sa_access else ("Folder podpięty — aktualizacja audytu zadziała. Otwieranie pojedynczych plików wymaga udostępnienia folderu dla %s (rola Czytelnik)." % _SA_EMAIL)
    return JSONResponse({"ok": True, "name": name, "sa_access": sa_access, "note": note})


@router.post("/api/analizator/update/{aid}")
def api_update(aid: str, user=Depends(get_current_user)):
    """Tryb przyrostowy — dograj nowe dokumenty z podpiętego folderu do istniejącego audytu."""
    if not re.match(r"^AUDYT_[A-Za-z0-9_]+$", aid):
        return JSONResponse({"error": "Zła nazwa audytu."}, status_code=400)
    src = _load_sources().get(aid)
    if not src or not src.get("folder_id"):
        return JSONResponse({"error": "Najpierw podepnij źródłowy folder Drive (pole w sekcji „Co przeanalizowane”), żeby było skąd dograć nowe dokumenty."}, status_code=200)
    url = "https://drive.google.com/drive/folders/%s" % src["folder_id"]
    jid = uuid.uuid4().hex[:12]
    ENG.jset(jid, pct=0, stage="Inicjalizacja", started=int(time.time()), mode="update", url=url)
    threading.Thread(target=ENG.run_update, args=(jid, aid, url), daemon=True).start()
    return JSONResponse({"job_id": jid})
