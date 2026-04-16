"use strict";

const {
  makeWASocket,
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestWaWebVersion,
} = require("@whiskeysockets/baileys");
const axios = require("axios");
const express = require("express");
const pino = require("pino");
const qrcode = require("qrcode-terminal");

const log = pino({
  level: process.env.LOG_LEVEL || "info",
  transport: process.env.NODE_ENV !== "production"
    ? { target: "pino-pretty" }
    : undefined,
});

const FASTAPI_WEBHOOK_URL =
  process.env.FASTAPI_WEBHOOK_URL || "http://api:8000/api/v1/messages/inbound";
const PORT = parseInt(process.env.PORT || "3000", 10);
const SESSIONS_DIR = "/app/sessions";

// ── Express server (send-message API + health) ────────────────────────────────
const app = express();
app.use(express.json({ limit: "10kb" }));   // DRC payload constraint

let sock = null;

/**
 * Health endpoint — used by Docker healthcheck and FastAPI adapter.
 */
app.get("/health", (req, res) => {
  res.json({
    status: "ok",
    connected: sock?.user != null,
    jid: sock?.user?.id ?? null,
  });
});

/**
 * Send a text message to a WhatsApp number.
 * Called by MessagingAdapter (Baileys mode) from FastAPI.
 *
 * Body: { "phone": "243XXXXXXXXX", "message": "Mbote!" }
 */
app.post("/send", async (req, res) => {
  const { phone, message } = req.body;

  if (!phone || !message) {
    return res.status(400).json({ error: "phone and message are required" });
  }
  if (!sock?.user) {
    return res.status(503).json({ error: "WhatsApp not connected" });
  }

  try {
    const jid = `${phone}@s.whatsapp.net`;
    await sock.sendMessage(jid, { text: message });
    log.info({ phone, chars: message.length }, "message_sent");
    res.json({ success: true });
  } catch (err) {
    log.error({ phone, err: err.message }, "send_failed");
    res.status(500).json({ error: err.message });
  }
});

app.listen(PORT, () => {
  log.info({ port: PORT }, "baileys_bridge_listening");
});

// ── WhatsApp Connection ───────────────────────────────────────────────────────
async function connectToWhatsApp() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSIONS_DIR);

  // Fetch latest WA Web version to avoid protocol rejection (405 errors)
  let version;
  try {
    const versionInfo = await fetchLatestWaWebVersion({});
    version = versionInfo.version;
    log.info({ version }, "fetched_wa_web_version");
  } catch (err) {
    log.warn({ err: err.message }, "version_fetch_failed_using_default");
  }

  sock = makeWASocket({
    auth: state,
    logger: pino({ level: process.env.BAILEYS_LOG_LEVEL || "silent" }),
    browser: ["MBB ya Kin", "Chrome", "1.0.0"],
    syncFullHistory: false,
    generateHighQualityLinkPreview: false,
    ...(version && { version }),
  });

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      log.info("qr_code_generated — scan with WhatsApp on your phone");
      qrcode.generate(qr, { small: true });
    }

    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = code !== DisconnectReason.loggedOut;
      log.warn({ code, shouldReconnect }, "connection_closed");

      if (shouldReconnect) {
        // Exponential backoff (max 30s) — handles DRC instability
        const delay = Math.min(5000 * (1 + Math.random()), 30000);
        log.info({ delay_ms: delay }, "reconnecting");
        setTimeout(connectToWhatsApp, delay);
      } else {
        log.error("logged_out — delete sessions/ folder and restart to re-scan QR");
      }
    } else if (connection === "open") {
      log.info({ jid: sock.user?.id }, "whatsapp_connected");
    }
  });

  sock.ev.on("creds.update", saveCreds);

  // ── Inbound Message Handler ─────────────────────────────────────────────────
  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;

    for (const msg of messages) {
      // Skip outbound messages
      if (msg.key.fromMe) continue;

      const phone = msg.key.remoteJid
        .replace("@s.whatsapp.net", "")
        .replace("@g.us", "");

      const text =
        msg.message?.conversation ||
        msg.message?.extendedTextMessage?.text ||
        null;

      const msgType = msg.message?.conversation
        ? "text"
        : msg.message?.audioMessage
        ? "audio"
        : msg.message?.imageMessage
        ? "image"
        : "other";

      const payload = {
        phone,
        message_id: msg.key.id,
        content: text ?? "",
        type: msgType,
        timestamp: new Date(Number(msg.messageTimestamp) * 1000).toISOString(),
      };

      log.info({ phone, type: msgType, message_id: payload.message_id }, "inbound_message");

      // Forward to FastAPI with retry (up to 3 attempts — DRC network resilience)
      let attempt = 0;
      while (attempt < 3) {
        try {
          await axios.post(FASTAPI_WEBHOOK_URL, payload, {
            timeout: 8000,
            headers: { "Content-Type": "application/json" },
          });
          break;
        } catch (err) {
          attempt++;
          log.warn(
            { attempt, err: err.message, phone },
            "fastapi_forward_failed"
          );
          if (attempt < 3) {
            await new Promise((r) => setTimeout(r, 2000 * attempt));
          } else {
            log.error({ phone, message_id: payload.message_id }, "fastapi_forward_exhausted");
          }
        }
      }
    }
  });
}

connectToWhatsApp().catch((err) => {
  log.error({ err: err.message }, "fatal_baileys_init_error");
  process.exit(1);
});
