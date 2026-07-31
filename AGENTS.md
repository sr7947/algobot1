# AGENTS.md

## Cursor Cloud specific instructions

### Repository layout

The git root is `/workspace`. The application lives in `indian-fno-agent/` (FastAPI backend + React/Vite dashboard).

### System services (not started automatically)

This Cloud VM does not use systemd for PostgreSQL/Redis. After each fresh VM boot, start them manually:

```bash
sudo pg_ctlcluster 16 main start
redis-server --daemonize yes
redis-cli ping   # expect PONG
```

PostgreSQL user/db (`fnoagent` / `changeme` / `fnoagent`) and schema (`scripts/init_db.sql`) are created during initial environment setup. Re-run init only on a fresh database:

```bash
sudo -u postgres psql -d fnoagent -f indian-fno-agent/scripts/init_db.sql
```

### Environment file

Copy `indian-fno-agent/.env.example` to `indian-fno-agent/.env` if missing. For local dev without broker credentials, set `BROKER=paper` and `TRADING_MODE=PAPER`.

### Python dependencies

- Use the venv at `indian-fno-agent/.venv`.
- `requirements.txt` pins `pandas-ta==0.3.14b`, which is unavailable on PyPI; install `pandas-ta==0.4.71b0` instead (the update script handles this).
- `pyproject.toml` lists `ta-lib`, but the code uses `pandas_ta`; follow `requirements.txt`, not `ta-lib`.

### Running the stack

From `indian-fno-agent/`:

```bash
.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

From `indian-fno-agent/dashboard/`:

```bash
npm run dev    # http://localhost:5173 — proxies /api and /ws to port 8000
```

Health check: `curl http://localhost:8000/health`

### Lint and tests

```bash
cd indian-fno-agent
.venv/bin/ruff check .
.venv/bin/pytest tests/unit/ -v
```

- `tests/unit/test_charges.py` and `tests/unit/test_delta_leverage.py` pass without external services.
- `tests/unit/test_risk_engine.py` currently fails due to a Pydantic `RiskState` model definition issue (pre-existing).

### Docker Compose gaps

`docker-compose.yml` references `scheduler.celery_app` (module missing) and `dashboard/Dockerfile.dashboard` (file missing). Use postgres/redis services only, or run API/dashboard locally as above.

### Optional external credentials

Telegram, Gemini, Angel One, and Delta Exchange are optional for paper-mode dashboard/API demos. The API starts Telegram polling when `TELEGRAM_BOT_TOKEN` is set.
