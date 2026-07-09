# MBB ya Kin — Pilot Runbook
**Phase 1.E — Sprint 1.10**
**Version:** 1.0
**Audience:** Hub Team, DevOps, Lab Team

---

> **Recovery status warning:** This runbook is historical and is not current launch guidance. The project remains in recovery/stabilization mode and is not production-ready, pilot-ready, feature-ready, or fully stabilized. Step 13 validated only the controlled backend MVP webhook pipeline, and Step 14A validated Dashboard/API read safety. Live Baileys inbound, real WhatsApp outbound, external AI provider integration, production compose/nginx behavior, and payment/CRM/conversion/relance/escalation domains are not currently validated.

---

## 1. Pre-Launch Checklist

Complete all items before sending the first pilot message.

### 1.1 Infrastructure
- [ ] VPS provisioned: 4 vCPU, 16 GB RAM, 100 GB SSD (Ubuntu 22.04 LTS)
- [ ] Domain DNS configured: `api.mbb.cd → VPS IP`, `dashboard.mbb.cd → VPS IP`
- [ ] SSL certificate obtained (Let's Encrypt via Certbot)
- [ ] Docker Engine + Docker Compose v2 installed
- [ ] Firewall: ports 80, 443 open; 8000/8501/3000 closed externally

### 1.2 Secrets
- [ ] All 12 secret files written to `/run/secrets/` on VPS:
  - `postgres_db`, `postgres_user`, `postgres_password`
  - `jwt_secret` (≥ 32 chars, random)
  - `claude_api_key`
  - `airtable_api_key`
  - `whatsapp_api_token`, `whatsapp_api_secret`, `whatsapp_verify_token`
  - `orange_money_key`, `airtel_money_key`, `mpesa_key`
  - `payment_webhook_secret`
  - `baileys_webhook_secret`

### 1.3 Database
- [ ] `alembic upgrade head` run successfully
- [ ] Seed data loaded (product catalog, hub team accounts)
- [ ] PostgreSQL backup script configured: `pg_dump` every 6h via cron
- [ ] Redis AOF persistence confirmed: `CONFIG GET appendonly` returns `yes`

### 1.4 Application
- [ ] All Docker containers running: `docker compose ps` shows all healthy
- [ ] Health check passing: `curl https://api.mbb.cd/api/v1/health` → `{"status": "ok"}`
- [ ] WhatsApp webhook registered in Meta Dashboard → `https://api.mbb.cd/api/v1/messages/webhook`
- [ ] Webhook verification test passed (Meta sends GET, API returns challenge)
- [ ] Test message sent end-to-end: WhatsApp → Baileys/Official → FastAPI → Celery → DB
- [ ] Dashboard accessible: `https://dashboard.mbb.cd` (admin login works)

### 1.5 Monitoring
- [ ] Grafana accessible: `https://grafana.mbb.cd`
- [ ] Prometheus scraping API metrics: `up{job="mbb-api"} == 1`
- [ ] Loki receiving logs from all containers
- [ ] Alert rules configured (see Section 4)
- [ ] Grafana alerting channel configured (email or Slack)

### 1.6 Team Readiness
- [ ] Hub team briefed: how to monitor escalations in dashboard
- [ ] Hub team knows escalation response SLA: 30 min during business hours
- [ ] Lab team has reviewed all 9 relance templates (native tone approval)
- [ ] Rollback plan reviewed by all team leads (see Section 6)
- [ ] On-call schedule assigned for 2-week pilot

---

## 2. Deployment Steps

```bash
# 1. Pull latest code
git pull origin main

# 2. Build production images
docker compose -f docker-compose.yml -f docker-compose.prod.yml build

# 3. Start infrastructure (postgres, redis, nginx)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d postgres redis nginx

# 4. Run database migrations
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm api alembic upgrade head

# 5. Start application services
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api celery_worker celery_beat

# 6. Start dashboard and monitoring
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d dashboard prometheus grafana loki

# 7. Verify all services
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps

# 8. Smoke test
curl https://api.mbb.cd/api/v1/health
```

---

## 3. Daily Monitoring Checklist (Morning Review)

Run every morning during the 2-week pilot (10 min).

### 3.1 System Health
```bash
# Check all containers running
docker compose ps

# Check error rate (last 24h)
# In Grafana: Dashboard → MBB Overview → Error Rate panel
# Threshold: < 5%

# Check Celery queue depth
docker compose exec redis redis-cli llen celery
# Threshold: < 100 tasks queued
```

### 3.2 Business Metrics (Grafana)
Check these panels daily:
- **Automation rate**: conversations handled without escalation → target ≥ 80%
- **Opt-out rate (daily)**: customers who sent stop/arrête/boleka → threshold < 8%/day
- **Lead conversion**: hot+warm leads who placed orders → target ≥ 15%
- **Response time (p95)**: API response time → threshold < 60s
- **Escalation queue**: unresolved escalations → Hub team must clear within 30 min

### 3.3 Log Review
```bash
# Check for ERROR-level logs (last 24h)
docker compose logs api --since 24h | grep ERROR

# Check Celery task failures
docker compose logs celery_worker --since 24h | grep "FAILURE\|ERROR"

# Check Baileys connection status
curl https://api.mbb.cd/qr.json
# Expected: {"connected": true}
```

---

## 4. Alert Thresholds

| Metric | Warning | Critical | Action |
|--------|---------|----------|--------|
| p95 response time | > 30s | > 60s | Scale workers, check Claude API |
| Error rate | > 2% | > 5% | Check logs, consider rollback |
| Celery queue depth | > 50 | > 100 | Scale workers, check blackout mode |
| Opt-out rate (daily) | > 5% | > 8% | Review relance content, pause if needed |
| Escalation response time | > 20 min | > 30 min | Alert Hub team lead directly |
| Payment failure rate | > 10% | > 15% | Check Mobile Money API status |
| Redis memory usage | > 70% | > 80% | Flush old sessions, increase memory |
| PostgreSQL connections | > 70% | > 80% | Increase pool size, check leaks |
| WhatsApp QR status | disconnected | — | Reconnect via /qr dashboard page |

---

## 5. Escalation Procedures

### 5.1 Issue Classification

| Class | Definition | SLA | Who |
|-------|-----------|-----|-----|
| **P0** | System down, no messages processed | Fix within 1h, 24/7 | DevOps on-call |
| **P1** | >5% error rate or >60s response time | Fix within 4h | DevOps + Backend |
| **P2** | Feature broken (payment, escalation, etc.) | Fix within 24h | Backend dev |
| **P3** | UI/cosmetic, non-blocking | Fix this sprint | Backend dev |

### 5.2 Contact Tree

```
P0 — System Down
  → DevOps on-call (phone)
  → If not reached: CTO (phone)
  → Initiate rollback immediately (Section 6)

P1 — High Error Rate
  → DevOps on-call (WhatsApp)
  → Backend lead (WhatsApp)
  → Assess: scale vs. rollback vs. patch

P2 — Feature Bug
  → Backend dev (WhatsApp)
  → Hub team lead notified
  → Create ticket, fix in <24h

Hub Escalation Overload
  → Hub team lead redistributes
  → If > 30 min queue: pause relance sending
    (Admin dashboard → Settings → pause_relances = true)
```

### 5.3 How to Pause Relances (Emergency)
```bash
# Via admin API
curl -X POST https://api.mbb.cd/api/v1/admin/config \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"key": "pause_relances", "value": true}'

# Via Celery Beat (stop schedule)
docker compose exec celery_beat celery -A app.tasks.celery_app beat --detach --pidfile= 2>/dev/null
```

---

## 6. Rollback Plan

Target: revert to previous version in < 5 minutes.

### 6.1 Quick Rollback (Same Server)
```bash
# 1. Identify last good commit
git log --oneline -10

# 2. Checkout last good version
git checkout <last_good_commit>

# 3. Rebuild and redeploy
docker compose -f docker-compose.yml -f docker-compose.prod.yml build api celery_worker
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps api celery_worker

# 4. Verify health
curl https://api.mbb.cd/api/v1/health

# 5. Alert team the rollback is live
```

### 6.2 Database Rollback (If Schema Changed)
```bash
# Only if alembic migration was run
docker compose exec api alembic downgrade -1

# Verify DB integrity
docker compose exec postgres psql -U mbb -d mbb -c "\dt"
```

### 6.3 Rollback Decision Criteria
Roll back immediately if:
- P95 response time > 120s for > 5 consecutive minutes
- Error rate > 20% for > 2 minutes
- Database unreachable for > 2 minutes
- Payment processing fully broken
- Data corruption suspected

---

## 7. Communication Plan

### 7.1 Pilot Start Announcement (to Hub Team)
> "The MBB bot pilot is live. For the next 2 weeks, the bot will handle initial customer contact automatically. You will receive escalations in the dashboard when customers need human help. Please respond to escalations within 30 minutes. Report any issues to [DevOps contact]."

### 7.2 Downtime Notification (to Pilot Leads)
> "Our system is briefly unavailable for maintenance. We will be back shortly. Thank you for your patience! / Système en maintenance. Nous revenons très bientôt. Merci! / Mfumo umesimamishwa kwa matengenezo. Tutarudi hivi karibuni!"

### 7.3 Weekly Pilot Update (to Team)
Report each Friday during pilot:
- Total conversations this week
- Automation rate
- Conversion rate
- Opt-out rate
- Top escalation reasons
- Bugs found + fixed

---

## 8. Post-Pilot Retrospective (Week 24)

Schedule a 2-hour retrospective with all stakeholders.

### Agenda
1. **Metrics review** (30 min) — Compare actuals vs. Phase 1.E targets
2. **What worked** (20 min) — Highlight successes
3. **What didn't** (20 min) — Honest assessment of gaps
4. **Customer feedback** (20 min) — Opt-outs, tone issues, confusion points
5. **Phase 2 prioritization** (30 min) — What to build next based on learnings

### Data to Collect Before Retrospective
```sql
-- Total conversations
SELECT COUNT(*) FROM conversations WHERE created_at >= pilot_start_date;

-- Automation rate
SELECT 1 - (COUNT(CASE WHEN stage = 'escalated' THEN 1 END)::float / COUNT(*))
FROM conversations WHERE created_at >= pilot_start_date;

-- Conversion rate
SELECT COUNT(CASE WHEN stage = 'converted' THEN 1 END)::float /
       COUNT(CASE WHEN score IN ('hot', 'warm') THEN 1 END)
FROM leads WHERE created_at >= pilot_start_date;

-- Opt-out rate
SELECT COUNT(CASE WHEN opt_out_flag THEN 1 END)::float / COUNT(*)
FROM customers WHERE created_at >= pilot_start_date;
```
