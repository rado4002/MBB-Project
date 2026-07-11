# ─────────────────────────────────────────────────────────────────────────────
# MBB ya Kin — Baileys WhatsApp Bridge (Dev Mode)
# ─────────────────────────────────────────────────────────────────────────────

FROM node:20-alpine

RUN apk add --no-cache \
    python3 \
    make \
    g++ \
    git \
    libc6-compat

WORKDIR /app

COPY package.json .
# Use HTTPS instead of SSH for GitHub git deps (no SSH keys in container)
RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" \
    && git config --global url."https://github.com/".insteadOf "git@github.com:" \
    && npm install --omit=dev

COPY src/ ./src/

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD wget -qO- http://localhost:3000/health || exit 1

CMD ["node", "src/index.js"]
