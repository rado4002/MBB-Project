# ─────────────────────────────────────────────────────────────────────────────
# MBB ya Kin — Baileys WhatsApp Bridge (Dev Mode)
# ─────────────────────────────────────────────────────────────────────────────

FROM node:20-alpine

RUN apk add --no-cache \
    python3 \
    make \
    g++ \
    libc6-compat

WORKDIR /app

COPY package.json .
RUN npm install --production

COPY src/ ./src/

# Session data volume
VOLUME ["/app/sessions"]

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD wget -qO- http://localhost:3000/health || exit 1

CMD ["node", "src/index.js"]
