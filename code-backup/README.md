# code-backup — snapshoty kodu z ai-server-01

Pliki żyją na serwerze ai-server-01 POZA gitem (serwer trzyma kopie robocze bez .git).
To są punktowe backupy do continuity multi-machine. NIE edytować tu — edytować na serwerze, potem re-snapshot.

## Analizator przetargów (platforma.wskonsorcjum.pl/dashboard) — 2026-06-03
- `audyt/scripts/dashboard.py` — parser xlsx audytów (fix: czyste nazwy, wartości z banera, terminy, data_only=True)
- `audyt/app/server.py` — silnik (list_audits/full_audit/run_demo/run_real), reużywa dashboard.parse()
- `siwz-agent/backend/modules/analizator/` — moduł platformy: app.html (SPA pulpit), router.py (API /api/analizator/*), manifest.py
