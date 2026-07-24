# Phase 1.E — Validation & Launch

**MBB ya Kin — Sub-Phase Specification**

> **Current-status boundary:** This is a historical Phase 1.E plan and implementation record, not current launch evidence. Recovery and local stabilization are nearly complete, but the project is not publicly deployed, production-ready, or pilot-ready. The controlled Baileys local flow and isolated production-like runtime passed; public hosting, DNS, ports 80/443, permanent secrets, CA-issued TLS, renewal/reload, and public deployment validation remain deferred.

| Field | Value |
|-------|-------|
| **Phase** | 1.E |
| **Name** | Validation & Launch |
| **Weeks** | 21–24 (4 weeks) |
| **Sprints** | 1.9, 1.10 |
| **Modules** | All M1–M9 (integration) |
| **Status** | Historical implementation record; public deployment and pilot validation deferred |

---

## 1. Goal

Historical target: validate the entire system through integration testing, security audit, load testing, and native tone review, then migrate to a publicly approved WhatsApp transport and launch a 2-week pilot with 100–150 real leads.

**Milestone:** System passes all quality gates. Pilot achieves 80%+ automation rate and 15%+ conversion rate with < 8% opt-out.

**The Kinshasa Test:** 100 real customers in Kinshasa interact with the bot over 2 weeks during multiple power outages → 80%+ conversations handled automatically → 15%+ place orders → < 8% opt-out → Hub team confirms escalations work → Lab team approves tone → dashboard shows accurate real-time metrics.

---

## 2. Success Metrics (Stage Gate)

| Metric | Target | How to Measure |
|--------|--------|----------------|
| Concurrent Load | 100 conversations @ < 60s | Locust load test |
| Security Audit | 0 critical vulnerabilities | OWASP scan or external pen test |
| Automation Rate | 80–85% no-human-intervention | `1 - (escalations / total_conversations)` |
| Pilot Conversion | ≥ 15% qualified leads → order | `orders / leads WHERE score IN ('hot', 'warm')` |
| Pilot Satisfaction | < 8% opt-out, 0 negative reviews | Track opt-outs + manual feedback |

**Exit Criteria:** Pilot runs for 2 weeks with daily monitoring. Issue log shows < 5 critical bugs, all resolved within 24 hours.

---

## 3. Dependencies

| Dependency | Source | Status |
|------------|--------|--------|
| All M1–M9 modules implemented and individually tested | Phase 1.A–1.D | ⬜ |
| Dashboard operational with role-based access | Phase 1.D | ⬜ |
| WhatsApp Business API account approved | External (Meta) | ⬜ |
| Native speakers available for tone review | Team | ⬜ |
| Pilot lead list (100–150 contacts) | Hub Team | ⬜ |

---

## 4. Sprint 1.9 — Integration Testing + Pilot Preparation (Weeks 21–22)

### 4.1 Objective

Run comprehensive end-to-end tests, perform security audit, load test, and prepare all operational materials for pilot launch.

### 4.2 Tasks

| # | Task | Deliverable | Depends On | Status |
|---|------|-------------|------------|--------|
| 1 | End-to-end integration tests (message → lead → relance → conversion) | Automated test suite | All M1–M9 | ⬜ |
| 2 | Load test: simulate 100 concurrent conversations | Locust test script + results report | All | ⬜ |
| 3 | Blackout resilience test (full power cycle simulation) | Recovery validation report | M3 | ⬜ |
| 4 | Security audit: JWT, HMAC, rate limiting, secrets management | Audit report | M1, M7 | ⬜ |
| 5 | Performance tuning: Redis caching, DB query optimization | Sub-60s response validated | All | ⬜ |
| 6 | Prepare 3 relance templates per language (9 total) | Reviewed by native speakers | M6 | ⬜ |
| 7 | Native tone audit (cultural review by Congolese team) | Tone approval document | M4 | ⬜ |
| 8 | Create pilot runbook (monitoring, escalation, rollback) | Operations document | DevOps | ⬜ |

### 4.3 Integration Test Scenarios

| # | Scenario | Modules Tested | Expected Outcome |
|---|----------|---------------|------------------|
| 1 | New customer sends "Mbote" | M1, M2, M4 | Lingala detected, warm greeting, conversation created |
| 2 | Customer asks about product | M4, M5 | Qualification questions asked, lead created |
| 3 | Customer provides city + intent | M5 | Lead scored (hot/warm/cold), stage updated |
| 4 | Hot lead gets recommendation | M5, M4 | Product recommendation with price in CDF |
| 5 | Customer says "Oui nalingi" | M7 | Order created, payment options presented |
| 6 | Orange Money payment callback | M7 | Order confirmed, CRM synced |
| 7 | Customer goes silent for 24h | M6 | Relance #1 sent (value hook) |
| 8 | Customer says "arrête" | M4, M6 | Opt-out processed, relances cancelled |
| 9 | Voice note received | M8 | Escalation ticket created, Hub notified |
| 10 | Power outage (kill FastAPI) | M3 | Messages queued, recovered on restart |
| 11 | Claude API fails | M2, M4 | Circuit breaker trips, template fallback |
| 12 | Dashboard loads with data | M9 | Funnel, relance, language charts accurate |
| 13 | Admin toggles feature flag | M9 | Change takes effect without restart |
| 14 | Hub overrides lead status | M9 | Status updated, audit logged |
| 15 | Mixed language conversation | M2, M4 | Language detected correctly per message |

### 4.4 Load Test Configuration (Locust)

```python
# Pseudocode for Locust test
class WhatsAppUser(HttpUser):
    wait_time = between(3, 10)  # Simulate real user typing

    @task(3)
    def send_text_message(self):
        # Simulate inbound WhatsApp message
        self.client.post("/api/v1/messages", json={...})

    @task(1)
    def check_conversation(self):
        # Simulate conversation context check
        self.client.get(f"/api/v1/conversations/{conv_id}")

# Target: 100 concurrent users, 5-minute sustained load
# Threshold: p95 response time < 60s
```

### 4.5 Security Audit Checklist

| Category | Check | Status |
|----------|-------|--------|
| **Authentication** | JWT tokens validated on all protected endpoints | ⬜ |
| **Authentication** | Token expiry enforced (configurable TTL) | ⬜ |
| **Authorization** | Role-based access enforced (admin/hub/lab) | ⬜ |
| **Authorization** | No privilege escalation possible | ⬜ |
| **Webhook Security** | HMAC-SHA256 validation on payment callbacks | ⬜ |
| **Webhook Security** | HMAC-SHA256 validation on WhatsApp webhooks | ⬜ |
| **Rate Limiting** | Redis token bucket enforced (10 msg/min) | ⬜ |
| **Secrets** | No secrets in code or environment variables (Docker Secrets) | ⬜ |
| **Secrets** | `.env` files in `.gitignore` | ⬜ |
| **Input Validation** | Pydantic models validate all inputs | ⬜ |
| **SQL Injection** | SQLAlchemy parameterized queries only | ⬜ |
| **XSS** | No raw HTML rendering in Streamlit | ⬜ |
| **HTTPS** | TLS 1.2+ enforced in production (Nginx) | ⬜ |
| **Logging** | No PII in logs (phone numbers masked) | ⬜ |
| **Idempotency** | All POST/PUT endpoints handle duplicates | ⬜ |

### 4.6 Pilot Runbook (Table of Contents)

1. **Pre-Launch Checklist** — All services healthy, monitoring active
2. **Monitoring Dashboard** — Grafana alerts, key metrics to watch
3. **Escalation Procedures** — Who to contact for each issue type
4. **Rollback Plan** — How to revert to previous version in < 5 min
5. **Daily Review Process** — Morning check: errors, opt-outs, response times
6. **Issue Classification** — P0 (fix now), P1 (fix today), P2 (fix this sprint)
7. **Communication Plan** — How to notify users of downtime

### 4.7 Acceptance Criteria (Sprint 1.9)

- [ ] All 15 integration test scenarios pass
- [ ] 100 concurrent conversations handled with < 60s response time
- [ ] Zero message loss in full blackout simulation
- [ ] All 9 relance templates (3 languages × 3 attempts) approved by native reviewers
- [ ] Security audit shows no critical vulnerabilities
- [ ] Pilot runbook reviewed and approved by team
- [ ] Performance tuning achieves p95 < 60s under load

---

## 5. Sprint 1.10 — Production Migration + Pilot Launch (Weeks 23–24)

### 5.1 Objective

Switch from Baileys (dev) to WhatsApp Business API (official), onboard real leads, and run a 2-week monitored pilot.

### 5.2 Tasks

| # | Task | Deliverable | Depends On | Status |
|---|------|-------------|------------|--------|
| 1 | Register WhatsApp Business API account with Meta | Approved business number | External | ⬜ |
| 2 | Implement Official WhatsApp API adapter in M1 | `WHATSAPP_MODE=official` path | M1 | ⬜ |
| 3 | Test webhook: Meta → Nginx → FastAPI | Production message flow verified | Nginx + M1 | ⬜ |
| 4 | Configure production Nginx (SSL, rate limiting) | HTTPS on port 443 | DevOps | ⬜ |
| 5 | Deploy to production VPS | All containers running | Docker | ⬜ |
| 6 | Switch `WHATSAPP_MODE=official` in production | Go-live configuration | All | ⬜ |
| 7 | Onboard 100–150 pilot leads | Real conversations flowing | Hub Team | ⬜ |
| 8 | Set up production monitoring (Prometheus + Grafana + alerts) | Alerts on response time, errors | DevOps | ⬜ |
| 9 | Daily monitoring during pilot (2 weeks) | Issue log + fix cycle | All | ⬜ |
| 10 | Pilot retrospective + Phase 2 planning | Lessons learned document | All | ⬜ |

### 5.3 Production Migration Checklist

```
Pre-Migration:
  □ VPS provisioned (4 vCPU, 16GB RAM, 100GB SSD)
  □ Domain DNS configured (api.mbb.cd → VPS IP)
  □ SSL certificate obtained (Let's Encrypt or equivalent)
  □ Docker Secrets configured on production VPS
  □ PostgreSQL backup script configured (pg_dump every 6h)
  □ Redis AOF persistence confirmed

Migration Steps:
  1. □ Pull latest code: git pull origin main
  2. □ Build production images: docker compose -f docker-compose.yml -f docker-compose.prod.yml build
  3. □ Start infrastructure: postgres, redis, nginx
  4. □ Run database migrations: alembic upgrade head
  5. □ Start application: api (3 replicas), celery_worker, celery_beat
  6. □ Start dashboard: streamlit
  7. □ Start monitoring: prometheus, grafana, loki
  8. □ Verify health: GET https://api.mbb.cd/api/v1/health
  9. □ Configure WhatsApp webhook URL in Meta dashboard
  10. □ Send test message → verify end-to-end flow

Post-Migration:
  □ Monitor error rates for 1 hour
  □ Verify Celery Beat schedules are firing
  □ Confirm Redis session caching is active
  □ Test payment callback webhook
  □ Announce pilot start to Hub Team
```

### 5.4 Pilot Monitoring Dashboard

| Metric | Alert Threshold | Action |
|--------|----------------|--------|
| Response time (p95) | > 60s | Scale workers, check Claude API |
| Error rate | > 5% | Check logs, rollback if critical |
| Message queue depth | > 100 | Scale workers, check blackout |
| Opt-out rate | > 8% daily | Review relance content, pause if needed |
| Escalation response time | > 30 min | Alert Hub team lead |
| Payment failure rate | > 15% | Check Mobile Money API status |
| Redis memory usage | > 80% | Flush old sessions, increase memory |
| PostgreSQL connections | > 80% pool | Increase pool size, check connection leaks |

### 5.5 Pilot Success Criteria (Week 24 Review)

| Metric | Target | Actual | Status |
|--------|--------|--------|--------|
| Total conversations | 100–150 | — | ⬜ |
| Automation rate | 80–85% | — | ⬜ |
| Qualified leads | > 70% of conversations | — | ⬜ |
| Conversion rate | ≥ 15% of qualified leads | — | ⬜ |
| Opt-out rate | < 8% | — | ⬜ |
| Average response time | < 60s | — | ⬜ |
| Message loss | 0 | — | ⬜ |
| Critical bugs | < 5 (all resolved in 24h) | — | ⬜ |
| Hub team satisfaction | Positive | — | ⬜ |
| Native tone approval | Approved | — | ⬜ |

### 5.6 Acceptance Criteria (Sprint 1.10)

- [ ] Real customers receive responses in < 60s
- [ ] 80%+ of conversations handled without human intervention
- [ ] Zero data loss during pilot period
- [ ] Hub Team confirms escalation flow works correctly
- [ ] At least 15% of qualified leads convert to orders
- [ ] < 8% opt-out rate across pilot period
- [ ] All critical bugs fixed within 24 hours
- [ ] Pilot retrospective completed with lessons learned

---

## 6. Deliverables Checklist

| # | Deliverable | Sprint | Status | File |
|---|-------------|--------|--------|------|
| 1 | Integration test suite (15 scenarios) | 1.9 | ✅ Done | `backend/tests/integration/test_full_flow.py` |
| 2 | Payment flow integration tests | 1.9 | ✅ Done | `backend/tests/integration/test_payment_flow.py` |
| 3 | Relance flow integration tests | 1.9 | ✅ Done | `backend/tests/integration/test_relance_flow.py` |
| 4 | Locust load test script | 1.9 | ✅ Done | `tests/load/locustfile.py` |
| 5 | Blackout resilience test | 1.9 | ✅ Done (prior) | `backend/tests/test_blackout_simulation.py` |
| 6 | Security audit checklist (30 checks) | 1.9 | ✅ Done | `tests/security/audit_checklist.md` |
| 7 | 9 relance templates (3 langs × 3 attempts) | 1.9 | ✅ Created | `backend/app/modules/m6_relance/templates/` |
| 8 | Native tone approval | 1.9 | ⬜ Pending | Lab Team review required |
| 9 | Pilot runbook | 1.9 | ✅ Done | `docs/pilot_runbook.md` |
| 10 | Official WhatsApp API adapter | 1.10 | ✅ Done (prior) | `backend/app/adapters/messaging/whatsapp_official_adapter.py` |
| 11 | Production docker-compose overlay | 1.10 | ✅ Done | `docker-compose.prod.yml` |
| 12 | Production Nginx SSL config | 1.10 | ✅ Done | `nginx/conf.d/mbb.ssl.conf` |
| 13 | Monitoring + Grafana alerts | 1.10 | ⬜ Pending | Requires production VPS |
| 14 | Pilot results report | 1.10 | ⬜ Pending | After 2-week pilot |
| 15 | Phase 2 planning document | 1.10 | ⬜ Pending | Post-pilot retrospective |

---

## 7. File Map (Expected Output — Sprint 1.10 Additions)

```
backend/
├── app/
│   ├── adapters/
│   │   └── messaging/
│   │       ├── base.py             # MessagingAdapter interface
│   │       ├── baileys.py          # BaileysAdapter (dev)
│   │       └── official.py         # OfficialWhatsAppAdapter (prod)
│   └── ...

tests/
├── integration/
│   ├── test_full_flow.py           # End-to-end scenarios
│   ├── test_blackout.py            # Power outage simulation
│   ├── test_payment_flow.py        # Payment callbacks
│   └── test_relance_flow.py        # Relance scheduling
├── load/
│   ├── locustfile.py               # Load test definition
│   └── results/                    # Load test reports
└── security/
    └── audit_checklist.md          # Security audit results

docs/
├── pilot_runbook.md                # Operational procedures
├── pilot_results.md                # Pilot metrics + analysis
└── phase2_plan.md                  # Phase 2 planning
```

---

## 8. Risk Mitigation (Phase 1.E Specific)

| Risk | Impact | Mitigation |
|------|--------|------------|
| WhatsApp Business API approval delayed | Pilot launch blocked | Apply early (Week 17); extend Baileys pilot if needed |
| Production VPS performance issues | Slow responses | Pre-test with Locust; have scaling plan ready |
| Real users send unexpected content | Unhandled edge cases | Catch-all escalation; log everything; daily review |
| Power outage during pilot | Service disruption | Redis AOF + Docker restart policies; test in Sprint 1.9 |
| Hub team overwhelmed by escalations | Poor user experience | Set auto-reassign timer; prioritize by lead score |
| Pilot leads not engaged | Low data for validation | Have backup lead list; extend pilot if needed |

---

## 9. Post-Pilot: Phase 1 → Phase 2 Transition

After successful pilot completion:

1. **Retrospective** — What worked, what didn't, what surprised us
2. **Data Analysis** — MAPS insights from real conversations
3. **Metric Review** — Compare actual vs. target KPIs
4. **Bug Triage** — Categorize remaining issues for Phase 2
5. **Phase 2 Prioritization** — Based on pilot learnings, reorder Phase 2 sprints
6. **Team Scaling** — Identify skills needed for Phase 2 (voice processing, advanced ML)
