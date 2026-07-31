# Deployment Guide

## Local Development

### Prerequisites
- Python 3.11+
- Node.js 18+ (for dashboard)
- PostgreSQL 15+
- Redis 7+

### Using Docker Compose (Recommended)

```bash
# Start everything
docker-compose up -d

# View logs
docker-compose logs -f app

# Stop
docker-compose down
```

This starts: PostgreSQL, Redis, the trading agent API, and Telegram bot.

### Manual Setup

```bash
# 1. Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows

# 2. Install dependencies
pip install -e ".[dev]"

# 3. Set up database
psql -U postgres -f scripts/init_db.sql

# 4. Start Redis
redis-server

# 5. Start API
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# 6. Start dashboard (separate terminal)
cd dashboard
npm install
npm run dev
```

---

## Production Deployment

### Docker on VPS

1. **Provision a VPS** (DigitalOcean, AWS Lightsail, etc.)
   - Recommended: 2 vCPU, 4GB RAM, Ubuntu 22.04+

2. **Clone and configure**:
   ```bash
   git clone <repo> && cd indian-fno-agent
   cp .env.example .env
   # Edit .env with production credentials
   ```

3. **Build and run**:
   ```bash
   docker-compose -f docker-compose.yml up -d --build
   ```

4. **Set up SSL** (recommended for API):
   ```bash
   # Use Caddy or Nginx with Let's Encrypt
   ```

### Environment Variables for Production

```env
TRADING_MODE=LIVE          # ⚠️ Real money
DEBUG=false
LOG_LEVEL=WARNING

# Use strong passwords
DATABASE_URL=postgresql://user:strongpass@db:5432/fno_agent
REDIS_URL=redis://redis:6379/0
```

---

## Monitoring

### Health Check
```bash
curl http://localhost:8000/health
```

### Logs
- Application logs: `logs/` directory
- Audit trail: `logs/audit_YYYY-MM-DD.jsonl`
- Docker logs: `docker-compose logs -f app`

### Telegram Alerts
The bot sends alerts for:
- Kill switch activation
- SL/target hits
- Broker connection errors
- System errors

---

## Backup

### Database
```bash
# Daily backup
pg_dump -U postgres fno_agent > backup_$(date +%Y%m%d).sql
```

### Configuration
Always version control your `config/` YAML files but **never** commit `.env`.
