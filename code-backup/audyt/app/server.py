#!/usr/bin/env python3
"""
Analizator dokumentacji przetargowej WSK — backend (FastAPI).
- Serwuje SPA (static/index.html) w stylu ProcureAnalytica.
- /api/audits, /api/audit/{id}  -> realne dane z ~/audyt/outputs/AUDYT_*.xlsx (parser z dashboard.py)
- /api/analyze (POST {url, mode}) -> startuje job: realny run agenta `claude` headless ALBO tryb demo
- /api/progress/{job_id} (SSE)   -> strumień postępu na żywo (pasek + log etapów)

DANE POUFNE — tylko 127.0.0.1; publicznie wyłącznie za Cloudflare Access (@wskonsorcjum.pl).
"""
import os, sys, json, glob, time, uuid, threading, subprocess, queue, re, html, shutil
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse

BASE   = "/home/wskpawelw/audyt"
OUTPUTS= os.path.join(BASE, "outputs")
LOGS   = os.path.join(BASE, "logs")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
sys.path.insert(0, os.path.join(BASE, "scripts"))
import dashboard as D   # reuse parse()

app = FastAPI(title="Analizator przetargów WSK")

# ---------------------------------------------------------------- audyty (realne dane)
def audit_id(path): return os.path.splitext(os.path.basename(path))[0]

def _audit_xlsx_files():
    """Collect all audit xlsx files regardless of case (AUDYT_*, audyt_*, Audyt_*)."""
    seen = set()
    for pattern in ("AUDYT_*.xlsx", "audyt_*.xlsx", "Audyt_*.xlsx"):
        for p in glob.glob(os.path.join(OUTPUTS, pattern)):
            rp = os.path.realpath(p)
            if rp not in seen:
                seen.add(rp)
                yield p

def list_audits():
    items=[]
    for p in sorted(_audit_xlsx_files(), key=os.path.getmtime, reverse=True):
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

# ---------------------------------------------------------------- silnik (claude headless)
# dostrojenie 2026-08-12: audyt CKiK Gościno przy OCR 108 stron rysunków rozdął
# proces silnika do 28 GB RSS (vision kumuluje obrazy w RAM CLI) → globalny OOM
# killer ubił silnik. Że proces siedział w cgroupie siwz-backend.service
# (OOMPolicy=stop + KillMode=control-group), systemd ubił i zrestartował CAŁĄ
# platformę, a backend pokazał tylko mylące "kod 143 — sprawdź dostęp do Drive".
# Teraz: własny scope z limitem pamięci (OOM ubija wyłącznie silnik), trwały log
# per job i uczciwa diagnoza kodu wyjścia. Patrz [[project_analizator_platforma]].
ENGINE_MEM_MAX  = os.environ.get("AUDYT_MEM_MAX",  "20G")
ENGINE_MEM_HIGH = os.environ.get("AUDYT_MEM_HIGH", "14G")
ENGINE_SWAP_MAX = os.environ.get("AUDYT_SWAP_MAX", "1G")
# dostrojenie 2026-08-12: model byl zaszyty w trzech miejscach, wiec przy wyczerpanym limicie
# konta nie dalo sie dokonczyc audytu tanszym modelem bez edycji kodu. Domyslnie bez zmian
# (opus-5 — audyt przetargowy potrzebuje najlepszego vision), przelaczasz przez AUDYT_MODEL.
MODEL_CONFIG = os.path.join(BASE, "config", "audit_model.json")
MODEL_ALIASES = {
    "opus": {"provider": "claude", "model": "claude-opus-5"},
    "gemini": {"provider": "gemini", "model": os.environ.get("AUDYT_GEMINI_MODEL", "gemini-2.5-pro")},
    "gpt": {"provider": "codex", "model": "gpt-5.6-sol"},
}

def engine_config(alias=None):
    """Konfiguracja czytana dla każdego joba, więc zmiana nie wymaga restartu.
    `alias` (dostrojenie 2026-09-14) wymusza konkretny silnik — używane przez łańcuch
    awaryjny po limicie Claude (patrz FALLBACK_CHAIN)."""
    fallback = os.environ.get("AUDYT_MODEL", "opus")
    selected = fallback
    if alias:
        cfg = MODEL_ALIASES.get(alias)
        return (alias, cfg) if cfg else (alias, {"provider": "claude", "model": alias})
    try:
        with open(MODEL_CONFIG, encoding="utf-8") as fh:
            selected = (json.load(fh).get("model") or fallback).strip().lower()
    except (FileNotFoundError, ValueError, TypeError, OSError):
        pass
    cfg = MODEL_ALIASES.get(selected)
    if cfg:
        return selected, cfg
    return selected, {"provider": "claude", "model": selected}

def claude_bin():
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "/home/wskpawelw/.local/bin/claude"

def gemini_bin():
    return os.environ.get("GEMINI_BIN") or shutil.which("gemini") or "/home/wskpawelw/.nvm/versions/node/v22.22.2/bin/gemini"

def codex_bin():
    return os.environ.get("CODEX_BIN") or shutil.which("codex") or "/home/wskpawelw/.local/bin/codex"

_scope_state={"ok":None}
def scope_ok():
    """Czy systemd-run --user --scope z limitem RAM faktycznie działa (D-Bus + delegacja
    cgroup). Sprawdzane raz — bez tego silnik odpalamy po staremu, byle w ogóle ruszył."""
    if _scope_state["ok"] is None:
        try:
            _scope_state["ok"] = shutil.which("systemd-run") is not None and subprocess.run(
                ["systemd-run","--user","--scope","--quiet","--collect",
                 "-p","MemoryMax=64M","/bin/true"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15).returncode==0
        except Exception:
            _scope_state["ok"]=False
    return _scope_state["ok"]

def engine_cmd(prompt, jid, alias=None):
    """Wybrany silnik headless w osobnym cgroup scope (alias= wymusza silnik)."""
    alias, cfg = engine_config(alias)
    provider, model = cfg["provider"], cfg["model"]
    if provider == "gemini":
        base=[gemini_bin(), "-p", prompt, "--output-format", "stream-json",
              "--model", model, "--sandbox", "--approval-mode", "yolo",
              "--include-directories", BASE]
    elif provider == "codex":
        base=[codex_bin(), "exec", "--json", "--model", model,
              "--sandbox", "workspace-write", "-C", "/home/wskpawelw",
              "--add-dir", BASE, "--skip-git-repo-check", prompt]
    else:
        base=[claude_bin(),"-p",prompt,"--output-format","stream-json","--verbose",
              "--dangerously-skip-permissions","--model",model]
    if scope_ok():
        return ["systemd-run","--user","--scope","--quiet","--collect",
                f"--unit=audyt-silnik-{jid}" + (f"-{alias}" if alias in MODEL_ALIASES and alias!=engine_config()[0] else ""),
                "-p",f"MemoryMax={ENGINE_MEM_MAX}",
                "-p",f"MemoryHigh={ENGINE_MEM_HIGH}",
                # bez tego pęczniejący silnik wypycha do swapu (3,8 GB na serwerze)
                # i zamula wszystko inne, zamiast po prostu paść na swoim limicie
                "-p",f"MemorySwapMax={ENGINE_SWAP_MAX}", *base]
    return base

def engine_env(alias=None):
    """Środowisko providera bez zapisywania lub logowania sekretów."""
    env = dict(os.environ)
    _, cfg = engine_config(alias)
    if cfg["provider"] == "claude":
        env.pop("ANTHROPIC_API_KEY", None)  # CLI używa subskrypcji OAuth
        # incydent 2026-09-11 (wymiennikownia #47): agent główny odpalił subagenta
        # audytu z run_in_background=true, a `claude -p` po 600 s ubija zadania w tle
        # ("Background tasks still running after 600s; terminating") → rc=0 bez xlsx.
        # 0 = czekaj na zadania w tle bez limitu.
        env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
    elif cfg["provider"] == "gemini":
        # Backend historycznie używa GOOGLE_API_KEY; Gemini CLI oczekuje
        # GEMINI_API_KEY dla trybu AI Studio.
        if not env.get("GEMINI_API_KEY") and env.get("GOOGLE_API_KEY"):
            env["GEMINI_API_KEY"] = env["GOOGLE_API_KEY"]
    return env

# ---- Łańcuch awaryjny po limicie Claude (dostrojenie 2026-09-14, incydent #47) ----
# 11.09 audyt wymiennikowni padł po 50 min na „You've hit your monthly spend limit" (429).
# Paweł: „jak się kończy token na claude, przełącz na gemini". Kolejność z env
# AUDYT_FALLBACK (domyślnie gemini, potem gpt/codex); pusty string = wyłączone.
FALLBACK_CHAIN=[a.strip() for a in os.environ.get("AUDYT_FALLBACK","gemini,gpt").split(",") if a.strip()]
LIMIT_RE=re.compile(r"spend limit|usage limit|rate limit|rate_limit|Credit balance is too low|"
                    r"monthly limit|quota|hit your", re.I)
AGENT_MD_SRC="/home/wskpawelw/.claude/agents/audyt-przetargowy.md"
AGENT_MD_COPY=os.path.join(BASE,"AGENT_AUDYT_PRZETARGOWY.md")

def limit_event(ev):
    """Czy zdarzenie stream-json Claude oznacza wyczerpany limit/kredyt (nie zwykły błąd
    narzędzia — tool_result cytujący stary log NIE ma tu odpalać)."""
    try:
        t=ev.get("type"); st=ev.get("subtype")
        if t=="result":
            if ev.get("api_error_status")==429: return True
            return bool(ev.get("is_error")) and bool(LIMIT_RE.search(str(ev.get("result") or "")))
        if t=="system" and st=="rate_limit_event":
            return (ev.get("rate_limit_info") or {}).get("status")=="rejected"
        if t=="assistant" and (ev.get("error")=="rate_limit" or ev.get("is_api_error_message")):
            txt=" ".join(c.get("text","") for c in ev.get("message",{}).get("content",[]) if isinstance(c,dict))
            return bool(LIMIT_RE.search(txt)) or ev.get("error")=="rate_limit"
    except Exception:
        pass
    return False

def provider_available(alias):
    """Czy silnik awaryjny ma binarkę i poświadczenia (bez logowania sekretów)."""
    _, cfg = engine_config(alias)
    p=cfg["provider"]
    if p=="gemini":
        return os.path.exists(gemini_bin()) and bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    if p=="codex":
        return os.path.exists(codex_bin())
    return os.path.exists(claude_bin())

def workdir_since(ts):
    """Katalogi robocze w ~/audyt zmienione od startu jobu — podpowiedź dla silnika
    awaryjnego, żeby nie pobierał/OCR-ował dokumentacji od zera."""
    out=[]
    for d in glob.glob(os.path.join(BASE,"*")):
        if os.path.isdir(d) and os.path.basename(d) not in ("outputs","logs","app","scripts","templates","config","dashboards","oferty","work","backups") \
           and not os.path.basename(d).startswith(("_","." )) and os.path.getmtime(d)>=ts-5:
            out.append(d)
    return sorted(out, key=os.path.getmtime, reverse=True)

def fallback_prompt(url, hint, workdirs):
    """Prompt niezależny od providera: Gemini/Codex nie mają subagenta Claude, więc
    dostają kopię instrukcji agenta jako plik do przeczytania i wykonania."""
    try:
        shutil.copyfile(AGENT_MD_SRC, AGENT_MD_COPY)
    except Exception:
        pass
    p=(f"Jesteś agentem audytu przetargowego WSK Konsorcjum. PEŁNA instrukcja agenta (persony ekspertów, "
       f"struktura 53 zakładek xlsx, żelazne zasady formuł/materiałów/pytań) jest w pliku {AGENT_MD_COPY} — "
       f"najpierw przeczytaj ją CAŁĄ i wykonaj w całości. Helpery openpyxl: {BASE}/_helpers.py, "
       f"przeliczanie formuł: python3 {BASE}/scripts/recalc.py <plik>. "
       f"Zadanie: pełny audyt przetargowy folderu Google Drive {url} (pobieranie: rclone, remote gdrive:, "
       f"--drive-root-folder-id=<ID folderu>). Bez halucynacji — ilości i ceny wyłącznie z dokumentów/katalogów. "
       f"Przeczytaj WSZYSTKIE pliki ze WSZYSTKICH podfolderów (też ZIP-y); rysunki i skany czytaj jako obrazy. "
       f"Pytanie do zamawiającego dopiero po przeszukaniu całej dokumentacji. "
       f"Wynik: xlsx w {OUTPUTS}/ (nazwa AUDYT_<POSTEPOWANIE>_<YYYYMMDD>_v1.xlsx) + recalc. "
       f"Pracuj do końca bez pytań do użytkownika.")
    if workdirs:
        p+=(f" Poprzedni silnik (Claude) przerwał pracę z powodu limitu; jego katalog roboczy z pobraną "
            f"dokumentacją, renderami stron i wynikami OCR: {workdirs[0]} — WYKORZYSTAJ go (sprawdź kompletność), "
            f"nie pobieraj i nie OCR-uj od zera.")
    if hint:
        p+=f" WSKAZÓWKA OPERATORA: {str(hint)[:2000]}"
    return p

def run_engine_generic(jid, alias, prompt, logf):
    """Uruchom dowolny silnik headless i strumieniuj log do jobu (format zdarzeń Gemini/Codex
    jest inny niż Claude — parsujemy tolerancyjnie). Zwraca kod wyjścia."""
    cmd=engine_cmd(prompt, jid, alias); env=engine_env(alias)
    if logf: logf.write(f"\n--- silnik awaryjny {alias}: start ---\n")
    try:
        proc=subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1, env=env)
    except Exception as e:
        jset(jid, log_add=f"ERR\tNie udało się uruchomić silnika {alias}: {e}"); return -1
    for line in proc.stdout:
        line=line.strip()
        if not line: continue
        if logf: logf.write(line+"\n")
        try: ev=json.loads(line)
        except Exception:
            jset(jid, log_add="MSG\t"+line[:140]); continue
        if not isinstance(ev,dict): continue
        item=ev.get("item") if isinstance(ev.get("item"),dict) else {}
        txt=(item.get("text") or ev.get("text") or ev.get("content") or ev.get("response") or "")
        if isinstance(txt,list): txt=" ".join(str(x.get("text","")) if isinstance(x,dict) else str(x) for x in txt)
        cmdtxt=item.get("command") or item.get("aggregated_output") or ""
        blob=(str(txt)+" "+str(cmdtxt))[:400]
        si=stage_for(blob)
        with LOCK: cur=JOBS[jid]["pct"]
        if si is not None and si<len(STAGES):
            name,pct=STAGES[si]; jset(jid, stage=name, pct=max(cur,min(pct,cur+3)))
        else:
            jset(jid, pct=min(96,cur+1))
        if cmdtxt: jset(jid, log_add=f"TOOL\t{alias}: {str(cmdtxt)[:90]}")
        elif str(txt).strip(): jset(jid, log_add="MSG\t"+str(txt).strip()[:140])
    rc=proc.wait()
    if logf: logf.write(f"\n--- silnik awaryjny {alias} zakończył: rc={rc} ({rc_opis(rc)}) ---\n")
    return rc

def engine_log(jid):
    """Trwały log surowego stdout silnika — JOBS ginie przy restarcie backendu."""
    try:
        os.makedirs(LOGS, exist_ok=True)
        return open(os.path.join(LOGS, f"job_{jid}.log"), "a", encoding="utf-8", buffering=1)
    except Exception:
        return None

def rc_opis(rc):
    """Czytelna diagnoza kodu wyjścia — bez zgadywania 'to pewnie Drive'."""
    if rc in (-9, 137):
        return (f"silnik ZABITY przez OOM — przekroczył limit pamięci ({ENGINE_MEM_MAX}). "
                f"Zwykle OCR zbyt wielu rysunków w jednym przebiegu; podziel dokumentację "
                f"albo podnieś AUDYT_MEM_MAX")
    if rc in (-15, 143):
        return ("silnik dostał SIGTERM (restart usługi backendu / ręczne zatrzymanie / OOM "
                "w cgroupie nadrzędnej) — audyt przerwany w połowie")
    if rc in (-6, 134):
        return "silnik przerwany (abort) — zajrzyj do logu joba"
    return f"kod {rc}"

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

def run_real(jid, url, hint=None):
    fid=extract_folder_id(url)
    jset(jid, log_add=f"INFO\tStart pełnej analizy. Folder ID: {fid}")
    if hint: jset(jid, log_add=f"INFO\tWskazówka operatora: {str(hint)[:200]}")
    before=set(_audit_xlsx_files())
    prompt=(f"Użyj subagenta audyt-przetargowy — uruchom go SYNCHRONICZNIE (run_in_background=false) "
            f"i czekaj na jego wynik; NIE kończ swojej tury, dopóki plik xlsx audytu nie istnieje w {OUTPUTS}/. "
            f"Zrób pełny audyt przetargowy folderu Google Drive: "
            f"{url} . Bez żadnych halucynacji — ilości i ceny wyłącznie z dokumentów/katalogów. "
            f"Zindeksuj i przeczytaj WSZYSTKIE pliki ze WSZYSTKICH podfolderów (rekurencyjnie, też ZIP-y); "
            f"pytanie do zamawiającego wolno zadać DOPIERO po przeszukaniu całej dokumentacji — "
            f"jeśli parametr jest w którymkolwiek pliku/rysunku, użyj go zamiast pytać. "
            f"Wygeneruj xlsx do {OUTPUTS}/ i przelicz formuły (recalc).")
    if hint:
        # dostrojenie 2026-09-14: opcjonalna wskazówka operatora (np. „OCR już gotowe w
        # katalogu roboczym X — nie pobieraj ponownie") przekazywana z /analyze
        prompt+=f" WSKAZÓWKA OPERATORA (przekaż subagentowi dosłownie): {str(hint)[:2000]}"
    cmd=engine_cmd(prompt, jid)
    logf=engine_log(jid)
    jset(jid, stage=STAGES[0][0], pct=3,
         log_add=f"INFO\tUruchamiam silnik (claude headless, limit RAM {ENGINE_MEM_MAX}). "
                 f"Log: {LOGS}/job_{jid}.log")
    # dostrojenie 2026-06-10: backend ładuje ANTHROPIC_API_KEY z .env → headless
    # płacił kredytami API ("Credit balance is too low"). Usuwamy klucz ze środowiska
    # procesu, żeby claude CLI użył subskrypcji (OAuth z ~/.claude/.credentials.json).
    env=engine_env()
    try:
        proc=subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1, env=env)
    except Exception as e:
        jset(jid, done=True, ok=False, log_add=f"ERR\tNie udało się uruchomić silnika: {e}")
        return
    tool_n=0; limit_hit=False; started_ts=time.time()
    for line in proc.stdout:
        line=line.strip()
        if not line: continue
        if logf: logf.write(line+"\n")
        try: ev=json.loads(line)
        except Exception: continue
        typ=ev.get("type")
        if limit_event(ev): limit_hit=True
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
    if logf:
        logf.write(f"\n--- silnik zakończył: rc={rc} ({rc_opis(rc)}) ---\n")
    after=set(_audit_xlsx_files())
    new=sorted(after-before, key=os.path.getmtime, reverse=True)
    # dostrojenie 2026-09-14: limit Claude (429 / spend limit) bez pliku → łańcuch awaryjny
    if not new and limit_hit and FALLBACK_CHAIN:
        jset(jid, log_add="ERR\tClaude: wyczerpany limit (429 / spend limit) — przełączam na silnik awaryjny.")
        wd=workdir_since(started_ts)
        for alias in FALLBACK_CHAIN:
            if not provider_available(alias):
                jset(jid, log_add=f"INFO\tSilnik {alias} niedostępny (brak binarki/klucza) — pomijam."); continue
            jset(jid, stage=f"Silnik awaryjny: {alias}", log_add=f"STAGE\tPrzełączam silnik → {alias}"
                 + (f" (katalog roboczy: {os.path.basename(wd[0])})" if wd else ""))
            rc=run_engine_generic(jid, alias, fallback_prompt(url, hint, wd), logf)
            after=set(_audit_xlsx_files())
            new=sorted(after-before, key=os.path.getmtime, reverse=True)
            if new:
                jset(jid, log_add=f"OK\tSilnik awaryjny {alias} wygenerował plik."); break
            jset(jid, log_add=f"ERR\tSilnik awaryjny {alias} zakończył (rc={rc}) bez pliku xlsx.")
    if logf: logf.close()
    if new:
        rid=audit_id(new[0])
        jset(jid, stage="Gotowe", pct=100, done=True, ok=True, result_id=rid,
             log_add=f"OK\tAudyt gotowy: {os.path.basename(new[0])}")
    elif limit_hit:
        jset(jid, stage="Limit silnika", pct=100, done=True, ok=False,
             log_add=f"ERR\tWyczerpany limit Claude i żaden silnik awaryjny nie dał pliku — audyt nie powstał, "
                     f"nic nie powiązano. Log: {LOGS}/job_{jid}.log")
    elif rc==0:
        # ok=False: bez pliku audyt NIE powstał — front nie może "zgadywać" najnowszego
        # audytu (incydent 2026-09-11: powiązał Dygowo z #47).
        jset(jid, stage="Bez pliku", pct=100, done=True, ok=False,
             log_add=f"ERR\tSilnik zakończył (kod 0), ale NIE powstał nowy plik xlsx — audyt nie został "
                     f"wygenerowany, nic nie powiązano. Sprawdź {LOGS}/job_{jid}.log i dostęp do Drive.")
    else:
        jset(jid, stage="Przerwane", pct=100, done=True, ok=False,
             log_add=f"ERR\tAudyt przerwany: {rc_opis(rc)}. Log: {LOGS}/job_{jid}.log")

def run_update(jid, aid, url):
    """Tryb przyrostowy: dograj nowe dokumenty do istniejącego audytu (nie od zera)."""
    fid=extract_folder_id(url)
    path=os.path.join(OUTPUTS, aid+".xlsx")
    if not os.path.exists(path):
        jset(jid, done=True, ok=False, log_add="ERR\tBrak istniejącego pliku audytu do aktualizacji."); return
    mtime0=os.path.getmtime(path)
    jset(jid, log_add=f"INFO\tStart aktualizacji {aid}. Folder ID: {fid}")
    prompt=(f"Użyj subagenta audyt-przetargowy w TRYBIE AKTUALIZACJI — uruchom go SYNCHRONICZNIE "
            f"(run_in_background=false) i czekaj na wynik; nie kończ tury przed zapisaniem pliku. "
            f"Istnieje już audyt: {path} . Folder Google Drive z dokumentacją: {url} . "
            f"Dograj TYLKO nowe/zmienione dokumenty do istniejącego pliku (nie rób audytu od nowa): "
            f"oznacz odpowiedzi na pytania, zaktualizuj termin/zakres/wadium jeśli się zmieniły, "
            f"dodaj zakładkę ZZ_AKTUALIZACJA z opisem co nowego. Nowe pytania do zamawiającego wolno dodać "
            f"DOPIERO po sprawdzeniu, że odpowiedzi nie ma w żadnym dokumencie (też w starych plikach i OCR rysunków). "
            # dostrojenie 2026-06-12: aktualizacja WINDY policzyła deltę +149k, ale
            # wpisała ją tylko jako baner — dashboard czyta LICZBY, więc widełki
            # zostały stare. Liczby mają być aktualizowane w obu miejscach.
            f"ŻELAZNA ZASADA WYCENY: jeśli nowe dokumenty zmieniają zakres lub ceny, ZAKTUALIZUJ LICZBY "
            f"(nie tylko dopisek/baner!): (1) w zakładce 25_KALKULACJA_WSTEPNA dodaj wiersz grupy dla nowych "
            f"zakresów i przelicz wiersz RAZEM, (2) w 01_STRESZCZENIE_ZARZADCZE zaktualizuj linię "
            f"'WARTOŚĆ SZACUNKOWA INWESTYCJI: X–Y mln PLN netto' — dashboard parsuje te dwa miejsca. "
            f"Starą wartość zostaw w dopisku '[było: ...]'. "
            f"Nadpisz ten sam plik i przelicz formuły (recalc).")
    cmd=engine_cmd(prompt, jid)
    logf=engine_log(jid)
    jset(jid, stage=STAGES[0][0], pct=3,
         log_add=f"INFO\tUruchamiam silnik (tryb aktualizacji, limit RAM {ENGINE_MEM_MAX}). "
                 f"Log: {LOGS}/job_{jid}.log")
    # jak w run_real: bez ANTHROPIC_API_KEY → subskrypcja zamiast kredytów API
    env=engine_env()
    try:
        proc=subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1, env=env)
    except Exception as e:
        jset(jid, done=True, ok=False, log_add=f"ERR\tNie udało się uruchomić silnika: {e}"); return
    for line in proc.stdout:
        line=line.strip()
        if not line: continue
        if logf: logf.write(line+"\n")
        try: ev=json.loads(line)
        except Exception: continue
        typ=ev.get("type")
        if typ=="assistant":
            for c in ev.get("message",{}).get("content",[]):
                if c.get("type")=="tool_use":
                    blob=(c.get("name","")+" "+json.dumps(c.get("input",{}),ensure_ascii=False))[:400]
                    si=stage_for(blob)
                    with LOCK: cur=JOBS[jid]["pct"]
                    if si is not None and si<len(STAGES):
                        jset(jid, stage=STAGES[si][0], pct=max(cur,min(STAGES[si][1], cur+4)))
                    else:
                        jset(jid, pct=min(96, cur+2))
                    inp=c.get("input",{})
                    hint=inp.get("file_path") or inp.get("command","") or inp.get("query","")
                    jset(jid, log_add=f"TOOL\t{c.get('name','tool')}: {str(hint)[:90]}")
                elif c.get("type")=="text" and c.get("text","").strip():
                    jset(jid, log_add="MSG\t"+c["text"].strip()[:140])
        elif typ=="result":
            break
    rc=proc.wait()
    if logf:
        logf.write(f"\n--- silnik zakończył: rc={rc} ({rc_opis(rc)}) ---\n"); logf.close()
    mtime1=os.path.getmtime(path) if os.path.exists(path) else mtime0
    if mtime1>mtime0:
        jset(jid, stage="Gotowe", pct=100, done=True, ok=True, result_id=aid,
             log_add="OK\tAudyt zaktualizowany (dograne nowe dokumenty).")
    elif rc==0:
        jset(jid, stage="Zakończono", pct=100, done=True, ok=True, result_id=aid,
             log_add="OK\tSilnik zakończył bez błędu, plik bez zmian — możliwe że brak nowych dokumentów w folderze.")
    else:
        jset(jid, stage="Przerwane", pct=100, done=True, ok=False, result_id=aid,
             log_add=f"ERR\tAktualizacja przerwana: {rc_opis(rc)}. Log: {LOGS}/job_{jid}.log")

def run_paczka(jid, prompt, wynik_dir):
    """Paczka ofertowa: agent wypełnia załączniki SWZ danymi firm i odkłada do Drive."""
    jset(jid, log_add=f"INFO\tStart przygotowania paczki ofertowej → {wynik_dir}")
    os.makedirs(wynik_dir, exist_ok=True)
    przed=set(glob.glob(os.path.join(wynik_dir,"*")))
    cmd=engine_cmd(prompt, jid)
    logf=engine_log(jid)
    jset(jid, stage="Czytanie SWZ i załączników", pct=3,
         log_add=f"INFO\tSilnik w cgroup scope (limit RAM {ENGINE_MEM_MAX}). Log: {LOGS}/job_{jid}.log")
    env=engine_env()
    try:
        proc=subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1, env=env)
    except Exception as e:
        jset(jid, done=True, ok=False, log_add=f"ERR\tNie udało się uruchomić silnika: {e}"); return
    for line in proc.stdout:
        line=line.strip()
        if not line: continue
        if logf: logf.write(line+"\n")
        try: ev=json.loads(line)
        except Exception: continue
        typ=ev.get("type")
        if typ=="assistant":
            for c in ev.get("message",{}).get("content",[]):
                if c.get("type")=="tool_use":
                    with LOCK: cur=JOBS[jid]["pct"]
                    jset(jid, pct=min(96, cur+2))
                    inp=c.get("input",{})
                    hint=inp.get("file_path") or inp.get("command","") or inp.get("query","")
                    jset(jid, log_add=f"TOOL\t{c.get('name','tool')}: {str(hint)[:90]}")
                elif c.get("type")=="text" and c.get("text","").strip():
                    jset(jid, log_add="MSG\t"+c["text"].strip()[:140])
        elif typ=="result":
            break
    rc=proc.wait()
    if logf:
        logf.write(f"\n--- silnik zakończył: rc={rc} ({rc_opis(rc)}) ---\n"); logf.close()
    nowe=set(glob.glob(os.path.join(wynik_dir,"*")))-przed
    if nowe:
        jset(jid, stage="Gotowe", pct=100, done=True, ok=True,
             log_add=f"OK\tPaczka gotowa: {len(nowe)} plików w {wynik_dir}")
    elif rc==0:
        jset(jid, stage="Zakończono", pct=100, done=True, ok=True,
             log_add=f"OK\tSilnik zakończył bez błędu, ale folder pusty — sprawdź {LOGS}/job_{jid}.log i Drive.")
    else:
        jset(jid, stage="Przerwane", pct=100, done=True, ok=False,
             log_add=f"ERR\tPaczka przerwana: {rc_opis(rc)}. Log: {LOGS}/job_{jid}.log")


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
