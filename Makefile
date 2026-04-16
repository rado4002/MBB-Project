# ─────────────────────────────────────────────────────────────────────────────
# MBB ya Kin — Makefile
# ─────────────────────────────────────────────────────────────────────────────

DC_BASE  := docker compose -f docker-compose.yml
DC_DEV   := $(DC_BASE) -f docker-compose.dev.yml
DC_PROD  := $(DC_BASE) -f docker-compose.prod.yml

.PHONY: help up down restart logs build rebuild secrets \
        shell-api shell-db redis-cli migrate seed \
        up-prod down-prod scale-prod

## ── Help ─────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "MBB ya Kin — Available commands"
	@echo "────────────────────────────────"
	@echo "  make setup        First-time setup: copy .env, generate secrets"
	@echo "  make up           Start dev environment (Baileys mode)"
	@echo "  make down         Stop dev environment"
	@echo "  make restart      Restart dev environment"
	@echo "  make logs         Follow all container logs"
	@echo "  make build        Build all images"
	@echo "  make rebuild      Force rebuild all images (no cache)"
	@echo ""
	@echo "  make shell-api    Bash shell inside running API container"
	@echo "  make shell-db     psql shell inside PostgreSQL container"
	@echo "  make redis-cli    Redis CLI inside Redis container"
	@echo ""
	@echo "  make migrate      Run Alembic database migrations"
	@echo "  make seed         Run database seed script"
	@echo ""
	@echo "  make up-prod      Start production environment (3 API replicas)"
	@echo "  make down-prod    Stop production environment"
	@echo ""

## ── First-Time Setup ─────────────────────────────────────────────────────────
setup:
	@[ -f .env ] || cp .env.example .env && echo ".env created from .env.example"
	@bash scripts/init_secrets.sh
	@echo ""
	@echo "Setup complete. Edit .env and ./secrets/*.txt, then run: make up"

## ── Dev Environment ──────────────────────────────────────────────────────────
up:
	$(DC_DEV) up -d
	@echo "Dev environment up. API: http://localhost/api/docs"
	@echo "Dashboard: http://localhost/dashboard/"
	@echo "Grafana:   http://localhost:3001/"

down:
	$(DC_DEV) down

restart:
	$(DC_DEV) restart

logs:
	$(DC_DEV) logs -f

logs-api:
	$(DC_DEV) logs -f api

logs-celery:
	$(DC_DEV) logs -f celery_worker celery_beat

## ── Build ────────────────────────────────────────────────────────────────────
build:
	$(DC_DEV) build

rebuild:
	$(DC_DEV) build --no-cache

## ── Shell Access ─────────────────────────────────────────────────────────────
shell-api:
	$(DC_DEV) exec api bash

shell-db:
	$(DC_DEV) exec postgres psql -U $$(cat secrets/postgres_user.txt) -d $$(cat secrets/postgres_db.txt)

redis-cli:
	$(DC_DEV) exec redis redis-cli

## ── Database ─────────────────────────────────────────────────────────────────
migrate:
	$(DC_DEV) exec api alembic upgrade head

migrate-down:
	$(DC_DEV) exec api alembic downgrade -1

seed:
	$(DC_DEV) exec api python scripts/seed_data.py

## ── Production ───────────────────────────────────────────────────────────────
up-prod:
	$(DC_PROD) up -d --scale api=3
	@echo "Production environment up (3 API replicas)"

down-prod:
	$(DC_PROD) down

## ── Health Check ─────────────────────────────────────────────────────────────
health:
	@curl -s http://localhost/health | python3 -m json.tool || echo "API not reachable"

ps:
	$(DC_DEV) ps
