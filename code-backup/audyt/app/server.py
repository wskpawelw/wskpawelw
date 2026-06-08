#!/usr/bin/env python3
"""
Analizator dokumentacji przetargowej WSK — backend (FastAPI).
- Serwuje SPA (static/index.html) w stylu ProcureAnalytica.
- /api/audits, /api/audit/{id}  -> realne dane z ~/audyt/outputs/AUDYT_*.xlsx (parser z dashboard.py)
- /api/analyze (POST {url, mode}) -> startuje job: realny run agenta `claude` headless ALBO tryb demo
- /api/progress/{job_id} (SSE)   -> strumień postępu na żywo (pasek + log etapów)

DANE POUFNE — tylko 127.0.0.1; publicznie wyłącznie za Cloudflare Access (@wskonsorcjum.pl).
"""
import os, sys, json, glob, time, uuid, threading, subprocess, queue, re, html
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse

BASE   = "/home/wskpawelw/audyt"
OUTPUTS= os.path.join(BASE, "outputs")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
sys.path.insert(0, os.path.join(BASE, "scripts"))
import dashboard as D   # reuse parse()

app = FastAPI(title="Analizator przetargów WSK")

# ---------------------------------------------------------------- audyty (realne dane)
def audit_id(path): return os.path.splitext(os.path.basename(path))[0]

def list_audits():
    items=[]
    for p in sorted(glob.glob(os.path.join(OUTPUTS,"AUDYT_*.xlsx")), key=os.path.getmtime, reverse=True):
        try:
            d=D.parse(p)
            rek=d["meta"].get("rekomendacja","")
            bid=rek.upper().startswith("SK") and not rek.upper().startswith("NIE")
            nod,ndo=d["value"].get("netto_od"),d["value"].get("netto_do")
            sev={}
            for r in d["risks"]: sev[r["sev"]]=sev.get(r["sev"],0)+1
            krit=sev.get("KRYTYCZNA",0)
            items.append({
                "id":audit_id(p),
                "project":d["meta"].get("project","Audyt przetargowy"),
                "bzp":d["meta"].get("bzp",""),
                "termin":d["meta"].get("termin",""),
                "wadium":d["meta"].get("wadium",""),
                "netto_od":nod,"netto_do":ndo,
                "brutto_od":d["value"].get("brutto_od"),"brutto_do":d["value"].get("brutto_do"),
                "mat_count":len(d["materials"]),"mat_total":d["mat_total"],
                "risks":len(d["risks"]),"krit":krit,"sev":sev,
                "bid":bool(bid),"rek":rek,
                "mtime":int(os.path.getmtime(p)),
            })
        except Exception as e:
            print("parse err",p,e, file=sys.stderr)
    return items

def full_audit(aid):
    p=os.path.join(OUTPUTS, aid+".xlsx")
    if not os.path.exists(p): return None
    d=D.parse(p)
    rek=d["meta"].get("rekomendacja","")
    d["meta"]["id"]=aid
    d["meta"]["bid"]=rek.upper().startswith("SK") and not rek.upper().startswith("NIE")
    try:
        d["coverage"]=D.coverage(p)
    except Exception as e:
        print("coverage err",p,e, file=sys.stderr); d["coverage"]={}
    sev={}
    for r in d["risks"]: sev[r["sev"]]=sev.get(r["sev"],0)+1
    d["sev"]=sev
    return d

@app.get("/api/audits")
def api_audits():
    return JSONResponse(list_audits())

@app.get("/api/audit/{aid}")
def api_audit(aid):
    d=full_audit(aid)
    if not d: return JSONResponse({"error":"nie znaleziono"},status_code=404)
    return JSONResponse(d)

# ---------------------------------------------------------------- joby analizy
JOBS={}   # job_id -> {pct,stage,log[],done,ok,result_id,started}
LOCK=threading.Lock()

STAGES=[
    ("Łączenie z folderem Google Drive",            4),
    ("Wczytywanie dokumentów (SWZ, OPZ, SST, przedmiar)", 14),
    ("OCR rysunków technicznych i decyzji (vision)", 30),
    ("Ekstrakcja materiałów i parametrów (PN-EN, klasy)", 46),
    ("Analiza rozbieżności (przedmiar vs projekt vs SST vs MKZ)", 60),
    ("Wycena rynkowa (Bistyp / Sekocenbud, widełki OD–DO)", 72),
    ("Panel 10 ekspertów branżowych", 84),
    ("Generowanie arkusza audytu (53 zakładki)", 93),
    ("Przeliczanie formuł i kontrola spójności", 97),
]

def jset(jid, **kw):
    with LOCK:
        j=JOBS.setdefault(jid,{"pct":0,"stage":"","log":[],"done":False,"ok":None,"result_id":None})
        for k,v in kw.items():
            if k=="log_add": j["log"].append({"t":int(time.time()),"m":v})
            else: j[k]=v

DRIVE_RE=re.compile(r"(?:folders/|id=)([A-Za-z0-9_\-]{20,})")
def extract_folder_id(url):
    m=DRIVE_RE.search(url or "")
    return m.group(1) if m else (url.strip() if url else "")

# --- tryb DEMO: płynny przebieg etapów (do podglądu UX, bez 20-min realnego runu) ---
def run_demo(jid, url):
    jset(jid, log_add=f"INFO\tStart analizy folderu: {url}")
    jset(jid, log_add="INFO\tTryb podglądu (demo) — symulacja przebiegu etapów silnika.")
    for name,pct in STAGES:
        jset(jid, stage=name, log_add="STAGE\t"+name)
        target=pct
        with LOCK: cur=JOBS[jid]["pct"]
        steps=max(1,(target-cur))
        for i in range(steps):
            time.sleep(0.10)
            jset(jid, pct=cur+i+1)
    jset(jid, stage="Gotowe", pct=100, done=True, ok=True, log_add="OK\tAnaliza zakończona — raport gotowy.")

# --- tryb REALNY: spawn `claude` headless z agentem audyt-przetargowy, parsowanie stream-json ---
TOOL_STAGE=[
    (re.compile(r"drive|rclone|gdrive|folder", re.I), 1),
    (re.compile(r"\.pdf|\.png|\.jpg|ocr|vision|rysun|decyzj", re.I), 2),
    (re.compile(r"materia|przedmiar|specyfik|PN-EN|klas", re.I), 3),
    (re.compile(r"rozbie|niezgod|vs |porówn", re.I), 4),
    (re.compile(r"wycen|bistyp|sekocenbud|cena|kalkulac", re.I), 5),
    (re.compile(r"ekspert|panel|opinia|prawnik|konstruktor", re.I), 6),
    (re.compile(r"openpyxl|xlsx|zakład|workbook|arkusz", re.I), 7),
    (re.compile(r"recalc|przelicz|formuł", re.I), 8),
]
def stage_for(text):
    for rx,idx in TOOL_STAGE:
        if rx.search(text or ""): return idx
    return None

def run_real(jid, url):
    fid=extract_folder_id(url)
    jset(jid, log_add=f"INFO\tStart pełnej analizy. Folder ID: {fid}")
    before=set(glob.glob(os.path.join(OUTPUTS,"AUDYT_*.xlsx")))
    prompt=(f"Użyj subagenta audyt-przetargowy. Zrób pełny audyt przetargowy folderu Google Drive: "
            f"{url} . Bez żadnych halucynacji — ilości i ceny wyłącznie z dokumentów/katalogów. "
            f"Wygeneruj xlsx do {OUTPUTS}/ i przelicz formuły (recalc).")
    import shutil
    claude_bin=os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "/home/wskpawelw/.local/bin/claude"
    cmd=[claude_bin,"-p",prompt,"--output-format","stream-json","--verbose",
         "--dangerously-skip-permissions","--model","opus"]
    jset(jid, stage=STAGES[0][0], pct=3, log_add="INFO\tUruchamiam silnik (claude headless)…")
    try:
        proc=subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1)
    except Exception as e:
        jset(jid, done=True, ok=False, log_add=f"ERR\tNie udało się uruchomić silnika: {e}")
        return
    tool_n=0
    for line in proc.stdout:
        line=line.strip()
        if not line: continue
        try: ev=json.loads(line)
        except Exception: continue
        typ=ev.get("type")
        if typ=="assistant":
            for c in ev.get("message",{}).get("content",[]):
                if c.get("type")=="tool_use":
                    tool_n+=1
                    blob=(c.get("name","")+" "+json.dumps(c.get("input",{}),ensure_ascii=False))[:400]
                    si=stage_for(blob)
                    if si is not None and si<len(STAGES):
                        name,pct=STAGES[si]
                        with LOCK: cur=JOBS[jid]["pct"]
                        jset(jid, stage=name, pct=max(cur,min(pct, cur+3)))
                    else:
                        with LOCK: cur=JOBS[jid]["pct"]
                        jset(jid, pct=min(96, cur+1))
                    short=c.get("name","tool")
                    inp=c.get("input",{})
                    hint=inp.get("file_path") or inp.get("command","") or inp.get("query","")
                    jset(jid, log_add=f"TOOL\t{short}: {str(hint)[:90]}")
                elif c.get("type")=="text" and c.get("text","").strip():
                    txt=c["text"].strip()
                    si=stage_for(txt)
                    if si is not None:
                        jset(jid, stage=STAGES[si][0])
                    jset(jid, log_add="MSG\t"+txt[:140])
        elif typ=="result":
            break
    rc=proc.wait()
    after=set(glob.glob(os.path.join(OUTPUTS,"AUDYT_*.xlsx")))
    new=sorted(after-before, key=os.path.getmtime, reverse=True)
    if new:
        rid=audit_id(new[0])
        jset(jid, stage="Gotowe", pct=100, done=True, ok=True, result_id=rid,
             log_add=f"OK\tAudyt gotowy: {os.path.basename(new[0])}")
    else:
        jset(jid, stage="Zakończono", pct=100, done=True, ok=(rc==0),
             log_add=f"{'OK' if rc==0 else 'ERR'}\tSilnik zakończył (kod {rc}). Nie wykryto nowego pliku — sprawdź logi/dostęp do Drive.")

@app.post("/api/analyze")
async def api_analyze(req: Request):
    body=await req.json()
    url=(body.get("url") or "").strip()
    mode=body.get("mode","demo")
    if not url:
        return JSONResponse({"error":"Podaj link do folderu Google Drive."}, status_code=400)
    jid=uuid.uuid4().hex[:12]
    jset(jid, pct=0, stage="Inicjalizacja", started=int(time.time()), mode=mode, url=url)
    target = run_real if mode=="real" else run_demo
    threading.Thread(target=target, args=(jid,url), daemon=True).start()
    return JSONResponse({"job_id":jid,"mode":mode})

@app.get("/api/progress/{jid}")
async def api_progress(jid):
    async def gen():
        import asyncio
        last=-1; last_len=0
        while True:
            with LOCK:
                j=JOBS.get(jid)
                snap=dict(j) if j else None
            if snap is None:
                yield "event: error\ndata: {}\n\n"; return
            if snap["pct"]!=last or len(snap["log"])!=last_len:
                last=snap["pct"]; last_len=len(snap["log"])
                yield "data: "+json.dumps(snap,ensure_ascii=False)+"\n\n"
            if snap.get("done"):
                yield "event: done\ndata: "+json.dumps(snap,ensure_ascii=False)+"\n\n"; return
            await asyncio.sleep(0.4)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

# ---------------------------------------------------------------- SPA
@app.get("/", response_class=HTMLResponse)
def index():
    return open(os.path.join(STATIC,"index.html"),encoding="utf-8").read()

@app.get("/healthz", response_class=PlainTextResponse)
def health(): return "ok"

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT","3043")))
