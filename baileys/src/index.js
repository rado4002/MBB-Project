"use strict";

const {
  makeWASocket,
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestWaWebVersion,
} = require("@whiskeysockets/baileys");
const axios = require("axios");
const express = require("express");
const fs = require("fs");
const pino = require("pino");

const log = pino({
  level: process.env.LOG_LEVEL || "info",
  transport: process.env.NODE_ENV !== "production"
    ? { target: "pino-pretty" }
    : undefined,
});

const FASTAPI_WEBHOOK_URL =
  process.env.FASTAPI_WEBHOOK_URL || "http://api:8000/api/v1/messages/baileys";
const WEBHOOK_SECRET = process.env.BAILEYS_WEBHOOK_SECRET || "";
const PORT = parseInt(process.env.PORT || "3000", 10);
const SESSIONS_DIR = "/app/sessions";

const PHONE_DIGITS_RE = /^[1-9][0-9]{6,14}$/;

function jidDomain(jid) {
  if (typeof jid !== "string") return "unknown";
  const at = jid.lastIndexOf("@");
  return at > 0 ? jid.slice(at + 1).toLowerCase() : "unknown";
}

function normalizePnJid(value) {
  if (typeof value !== "string") return null;

  const match = value.trim().match(/^\+?([0-9]{7,15})@s\.whatsapp\.net$/i);
  if (!match || !PHONE_DIGITS_RE.test(match[1])) return null;

  return `+${match[1]}`;
}

function normalizePhoneInput(value) {
  if (typeof value !== "string") return null;
  const digits = value.trim().replace(/^\+/, "");
  return PHONE_DIGITS_RE.test(digits) ? `+${digits}` : null;
}

function keyValue(message, field) {
  return message?.key?.[field] ?? message?.[field];
}

function messageContentType(message) {
  const content = message?.message;
  if (content?.conversation || content?.extendedTextMessage?.text) return "text";
  if (content?.audioMessage) return "audio";
  if (content?.imageMessage) return "image";
  if (content) return "other";
  return "none";
}

function messageKeyCount(message) {
  return message?.message && typeof message.message === "object"
    ? Object.keys(message.message).length
    : 0;
}

function resolveInboundIdentity(message) {
  const remoteJid = keyValue(message, "remoteJid");
  const domain = jidDomain(remoteJid);

  if (keyValue(message, "fromMe")) {
    return { skipReason: "from_me", jidDomain: domain };
  }
  if (!remoteJid) {
    return { skipReason: "missing_remote_jid", jidDomain: domain };
  }
  if (domain === "broadcast" || remoteJid === "status@broadcast") {
    return { skipReason: "status_or_broadcast", jidDomain: domain };
  }
  if (domain === "g.us") {
    return { skipReason: "group_message", jidDomain: domain };
  }
  if (domain !== "lid" && domain !== "s.whatsapp.net") {
    return { skipReason: "unsupported_jid_domain", jidDomain: domain };
  }

  const senderPn = normalizePnJid(keyValue(message, "senderPn"));
  if (senderPn) {
    return { phone: senderPn, source: "senderPn", jidDomain: domain };
  }

  const participantPn = normalizePnJid(keyValue(message, "participantPn"));
  const participant = keyValue(message, "participant");
  const participantDomain = jidDomain(participant);
  const participantContextIsValid =
    domain === "lid" && (!participant || participantDomain === "lid");
  if (participantPn && participantContextIsValid) {
    return { phone: participantPn, source: "participantPn", jidDomain: domain };
  }

  if (domain === "lid") {
    return { skipReason: "unresolved_lid", jidDomain: domain };
  }

  const directPhone = normalizePnJid(remoteJid);
  if (directPhone) {
    return { phone: directPhone, source: "remoteJid", jidDomain: domain };
  }

  return { skipReason: "invalid_phone_identity", jidDomain: domain };
}

function isWhatsAppSendEnabled() {
  return String(process.env.WHATSAPP_SEND_ENABLED || "false").toLowerCase() === "true";
}

function isBaileysConnectEnabled() {
  return String(process.env.BAILEYS_CONNECT_ENABLED || "true").toLowerCase() !== "false";
}

function safeStatusCategory(error) {
  const status = error?.response?.status;
  return Number.isInteger(status) ? Math.floor(status / 100) * 100 : "unknown";
}

function safeErrorType(error) {
  return error?.name || error?.code || "unknown_error";
}

function logSkippedInbound(identity) {
  log.info({ skip_reason: identity.skipReason }, "inbound_message_skipped");
}

function logInvalidInbound(skipReason) {
  log.info({ skip_reason: skipReason }, "inbound_message_skipped");
}

async function handleSend(req, res) {
  const { phone, message } = req.body || {};

  if (!phone || !message) {
    return res.status(400).json({ error: "phone and message are required" });
  }
  if (!isWhatsAppSendEnabled()) {
    log.warn({ safety_gate: "WHATSAPP_SEND_ENABLED", enabled: false }, "send_skipped_disabled");
    return res.status(200).json({
      success: false,
      skipped: true,
      reason: "whatsapp_send_disabled",
    });
  }
  if (!isBaileysConnectEnabled()) {
    log.warn({ connection_enabled: false }, "send_skipped_connection_disabled");
    return res.status(200).json({
      success: false,
      skipped: true,
      reason: "baileys_connect_disabled",
    });
  }
  if (!sock?.user || isReconnecting) {
    return res.status(503).json({ error: "WhatsApp not connected" });
  }

  const normalized = normalizePhoneInput(phone);
  if (!normalized) {
    return res.status(400).json({ error: "phone must be an international number" });
  }

  try {
    const jid = `${normalized.slice(1)}@s.whatsapp.net`;
    await sock.sendMessage(jid, { text: message });
    log.info({ content_type: "text", message_present: true }, "message_sent");
    return res.json({ success: true });
  } catch (err) {
    log.error({ error_type: safeErrorType(err), status_category: safeStatusCategory(err) }, "send_failed");
    return res.status(500).json({ error: "send failed" });
  }
}

async function handleInboundUpsert(event, postWebhook = axios.post) {
  if (!isBaileysConnectEnabled()) {
    log.info({ connection_enabled: false }, "inbound_skipped_connection_disabled");
    return;
  }
  if (!event || typeof event !== "object" || Array.isArray(event)) {
    logInvalidInbound("invalid_upsert_event");
    return;
  }

  const { messages, type } = event;
  if (!Array.isArray(messages)) {
    logInvalidInbound("invalid_messages_array");
    return;
  }
  if (type !== "notify") return;

  for (const msg of messages) {
    try {
      if (!msg || typeof msg !== "object" || Array.isArray(msg)) {
        logInvalidInbound("invalid_message_entry");
        continue;
      }
      if (!msg.key || typeof msg.key !== "object" || Array.isArray(msg.key)) {
        logInvalidInbound("invalid_message_key");
        continue;
      }
      if (typeof msg.key.id !== "string" || !msg.key.id.trim()) {
        logInvalidInbound("invalid_message_id");
        continue;
      }

      const identity = resolveInboundIdentity(msg);
      if (identity.skipReason) {
        logSkippedInbound(identity);
        continue;
      }

      const conversation = msg.message?.conversation;
      const extendedText = msg.message?.extendedTextMessage?.text;
      const text = typeof conversation === "string"
        ? conversation
        : typeof extendedText === "string" ? extendedText : null;
      if (text === null) {
        logInvalidInbound("unsupported_message_content");
        continue;
      }
      if (!text.trim()) {
        logInvalidInbound("empty_text_content");
        continue;
      }

      if (msg.messageTimestamp === null || msg.messageTimestamp === undefined) {
        logInvalidInbound("invalid_message_timestamp");
        continue;
      }
      const timestampMs = Number(msg.messageTimestamp) * 1000;
      if (!Number.isFinite(timestampMs)) {
        logInvalidInbound("invalid_message_timestamp");
        continue;
      }
      let timestamp;
      try {
        timestamp = new Date(timestampMs).toISOString();
      } catch {
        logInvalidInbound("invalid_message_timestamp");
        continue;
      }

      const payload = {
        customer_phone: identity.phone,
        whatsapp_message_id: msg.key.id,
        content: text,
        content_type: "text",
        timestamp,
      };

      log.info({
        event_type: "messages.upsert",
        jid_domain: identity.jidDomain,
        identity_source: identity.source,
        identity_resolved: true,
        content_type: "text",
        message_present: true,
        message_key_count: messageKeyCount(msg),
      }, "inbound_message");

      const headers = { "Content-Type": "application/json" };
      if (WEBHOOK_SECRET) headers["X-Webhook-Secret"] = WEBHOOK_SECRET;

      let attempt = 0;
      while (attempt < 3) {
        try {
          await postWebhook(FASTAPI_WEBHOOK_URL, payload, {
            timeout: 8000,
            headers,
          });
          break;
        } catch (err) {
          attempt++;
          log.warn({
            attempt,
            status_category: safeStatusCategory(err),
            identity_source: identity.source,
            jid_domain: identity.jidDomain,
          }, "fastapi_forward_failed");
          if (attempt < 3) {
            await new Promise((resolve) => setTimeout(resolve, 2000 * attempt));
          } else {
            log.error({
              identity_source: identity.source,
              jid_domain: identity.jidDomain,
              status_category: safeStatusCategory(err),
            }, "fastapi_forward_exhausted");
          }
        }
      }
    } catch (err) {
      log.error({
        skip_reason: "inbound_message_error",
        error_type: safeErrorType(err),
      }, "inbound_message_skipped");
    }
  }
}

function handleMessagesUpsertEvent(event, handler = handleInboundUpsert) {
  void Promise.resolve()
    .then(() => handler(event))
    .catch((err) => {
      log.error({
        skip_reason: "inbound_handler_rejected",
        error_type: safeErrorType(err),
      }, "inbound_handler_failed");
    });
}

// ── Express server (send-message API + health) ────────────────────────────────
const app = express();
app.use(express.json({ limit: "10kb" }));   // DRC payload constraint

let sock = null;
let currentQR = null;   // latest QR string from connection.update, cleared on connect
let qrTimestamp = 0;    // epoch ms of last QR update — used by polling endpoint
let isReconnecting = false;

/**
 * Root endpoint — service info and connection status.
 */
app.get("/", (req, res) => {
  const connectionEnabled = isBaileysConnectEnabled();
  res.json({
    service: "MBB ya Kin — Baileys WhatsApp Bridge",
    status: "running",
    endpoints: {
      root: "GET /",
      health: "GET /health",
      qr: "GET /qr",
      qr_json: "GET /qr.json",
      send: "POST /send",
      logout: "POST /logout",
    },
    whatsapp: {
      connection_enabled: connectionEnabled,
      connected: connectionEnabled && sock?.user != null,
      jid: connectionEnabled ? sock?.user?.id ?? null : null,
    },
  });
});

/**
 * Health endpoint — used by Docker healthcheck and FastAPI adapter.
 */
app.get("/health", (req, res) => {
  const connectionEnabled = isBaileysConnectEnabled();
  res.json({
    status: "ok",
    connection_enabled: connectionEnabled,
    connected: connectionEnabled && sock?.user != null,
    jid: connectionEnabled ? sock?.user?.id ?? null : null,
  });
});

/**
 * Send a text message to a WhatsApp number.
 * Called by MessagingAdapter (Baileys mode) from FastAPI.
 *
 * Body: { "phone": "243XXXXXXXXX", "message": "Mbote!" }
 */
app.post("/send", handleSend);

// QR status — polled by the dashboard SPA every 10 s
app.get("/qr.json", async (req, res) => {
  if (!isBaileysConnectEnabled()) {
    return res.json({
      connection_enabled: false,
      connected: false,
      jid: null,
      ts: 0,
      qrDataUrl: null,
    });
  }

  const payload = {
    connection_enabled: true,
    connected: sock?.user != null,
    jid: sock?.user?.id ?? null,
    ts: qrTimestamp,
    qrDataUrl: null,
  };

  if (currentQR) {
    try {
      const qrLib = require("qrcode");
      payload.qrDataUrl = await qrLib.toDataURL(currentQR, { width: 300, margin: 1 });
    } catch (e) {
      log.warn({ error_type: safeErrorType(e) }, "qr_dataurl_gen_failed");
    }
  }

  res.json(payload);
});

// Live QR dashboard — open http://localhost:3000/qr in a browser
app.get("/qr", (req, res) => {
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  if (!isBaileysConnectEnabled()) {
    return res.send("<!DOCTYPE html><html><body><p>WhatsApp connection is disabled.</p></body></html>");
  }
  res.send(`<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>MBB ya Kin — WhatsApp</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f0f2f5;display:flex;align-items:center;justify-content:center;
         min-height:100vh;padding:1rem}
    .card{background:#fff;border-radius:16px;box-shadow:0 2px 16px rgba(0,0,0,.12);
          padding:2.5rem 2rem;max-width:380px;width:100%;text-align:center}
    .card h1{font-size:1.25rem;font-weight:700;margin-bottom:.4rem;color:#111}
    .subtitle{font-size:.85rem;color:#666;margin-bottom:1.5rem}
    /* connected state */
    .connected-icon{font-size:3rem;margin-bottom:.75rem}
    .connected-title{font-size:1.4rem;font-weight:700;color:#25d366;margin-bottom:.4rem}
    .jid{font-size:.82rem;color:#555;margin-bottom:1.5rem;word-break:break-all}
    /* QR state */
    #qr-img{width:260px;height:260px;border-radius:8px;border:1px solid #e0e0e0;
            display:none;margin:0 auto 1rem}
    /* waiting state */
    .spinner{width:48px;height:48px;border:4px solid #e0e0e0;border-top-color:#25d366;
             border-radius:50%;animation:spin .8s linear infinite;margin:1rem auto}
    @keyframes spin{to{transform:rotate(360deg)}}
    /* buttons */
    .btn{display:inline-block;padding:.65rem 1.5rem;border-radius:8px;font-size:.9rem;
         font-weight:600;cursor:pointer;border:none;transition:opacity .15s}
    .btn:hover{opacity:.85}
    .btn-logout{background:#e53e3e;color:#fff;width:100%}
    .btn-refresh{background:#25d366;color:#fff;width:100%;margin-bottom:.75rem}
    .hint{font-size:.75rem;color:#aaa;margin-top:1.25rem}
    #error-msg{color:#e53e3e;font-size:.82rem;margin-top:.5rem;display:none}
  </style>
</head>
<body>
  <div class="card" id="card">
    <div id="view-checking">
      <div class="spinner"></div>
      <p class="subtitle">Connecting to WhatsApp bridge…</p>
    </div>
    <div id="view-connected" style="display:none">
      <div class="connected-icon">✅</div>
      <div class="connected-title">WhatsApp Connected!</div>
      <div class="jid" id="jid-label"></div>
      <button class="btn btn-logout" onclick="logout()">🔒 Logout &amp; Reset QR</button>
      <div id="error-msg"></div>
    </div>
    <div id="view-qr" style="display:none">
      <h1>Scan to Connect</h1>
      <p class="subtitle">Open WhatsApp → Linked Devices → Link a Device</p>
      <img id="qr-img" alt="WhatsApp QR code"/>
      <p class="subtitle" style="margin-bottom:0">Point your phone camera at the code above</p>
    </div>
    <div id="view-waiting" style="display:none">
      <div class="spinner"></div>
      <h1 style="margin-bottom:.4rem">Waiting for QR…</h1>
      <p class="subtitle">The bridge is starting up. This usually takes a few seconds.</p>
      <button class="btn btn-refresh" onclick="poll()">Refresh Now</button>
    </div>
    <div class="hint" id="hint">Auto-refreshes every 10 seconds</div>
  </div>
  <script>
    const views = ['checking','connected','qr','waiting'];
    function show(name) {
      views.forEach(v => document.getElementById('view-'+v).style.display = v===name ? '' : 'none');
    }
    async function poll() {
      try {
        const d = await fetch('/qr.json').then(r => r.json());
        if (d.connected) {
          document.getElementById('jid-label').textContent = 'Logged in as: ' + (d.jid || '');
          show('connected');
        } else if (d.qrDataUrl) {
          const img = document.getElementById('qr-img');
          img.src = d.qrDataUrl;
          img.style.display = 'block';
          show('qr');
        } else {
          show('waiting');
        }
      } catch(e) {
        document.getElementById('hint').textContent = '⚠️ Could not reach bridge — retrying…';
      }
    }
    async function logout() {
      const errEl = document.getElementById('error-msg');
      errEl.style.display = 'none';
      try {
        const r = await fetch('/logout', {method:'POST'});
        if (r.ok) { show('waiting'); setTimeout(poll, 2000); }
        else { errEl.textContent = 'Logout failed — try again'; errEl.style.display = ''; }
      } catch(e) {
        errEl.textContent = 'Network error during logout'; errEl.style.display = '';
      }
    }
    poll();
    setInterval(poll, 10000);
  </script>
</body>
</html>`);
});

/**
 * Logout endpoint — disconnect WhatsApp and clear session.
 */
app.post("/logout", async (req, res) => {
  if (!isBaileysConnectEnabled()) {
    return res.status(200).json({
      success: false,
      skipped: true,
      reason: "baileys_connect_disabled",
    });
  }

  if (!sock) {
    return res.status(400).json({ error: "No active WhatsApp connection" });
  }

  try {
    if (sock.user) {
      await sock.logout();
    }

    isReconnecting = true;
    sock = null;
    currentQR = null;

    // Wipe session files so next connection prompts a fresh QR
    try {
      fs.rmSync(SESSIONS_DIR, { recursive: true, force: true });
      fs.mkdirSync(SESSIONS_DIR, { recursive: true });
      log.info({ session_reset: true }, "session_dir_wiped");
    } catch (wipeErr) {
      log.warn({ error_type: safeErrorType(wipeErr) }, "session_wipe_failed");
    }

    // Trigger reconnect — new QR will appear on /qr shortly
    setTimeout(() => {
      connectToWhatsApp()
        .catch((err) => log.error({ error_type: safeErrorType(err) }, "post_logout_reconnect_failed"))
        .finally(() => { isReconnecting = false; });
    }, 1000);

    log.info("user_logged_out");
    res.json({
      success: true,
      message: "Logged out. New QR available at /qr in a few seconds.",
    });
  } catch (err) {
    log.error({ error_type: safeErrorType(err) }, "logout_failed");
    res.status(500).json({ error: "logout failed" });
  }
});

// ── Utility: Fetch version with timeout + retry ───────────────────────────────
async function fetchVersionWithRetry(maxAttempts = 3, timeoutMs = 10000) {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      log.info({ attempt, timeout_ms: timeoutMs }, "fetching_wa_web_version");

      // Wrap in timeout promise
      const versionPromise = fetchLatestWaWebVersion({});
      const timeoutPromise = new Promise((_, reject) =>
        setTimeout(() => reject(new Error(`Timeout after ${timeoutMs}ms`)), timeoutMs)
      );

      const versionInfo = await Promise.race([versionPromise, timeoutPromise]);
      log.info({ version: versionInfo.version }, "wa_web_version_fetched_successfully");
      return versionInfo.version;
    } catch (err) {
      log.warn(
        { attempt, max_attempts: maxAttempts, error_type: safeErrorType(err) },
        attempt === maxAttempts
          ? "version_fetch_exhausted_using_default"
          : "version_fetch_attempt_failed_retrying"
      );

      if (attempt < maxAttempts) {
        // Exponential backoff: 2s, 4s, 8s...
        const delayMs = Math.min(2000 * Math.pow(2, attempt - 1), 10000);
        log.info({ delay_ms: delayMs }, "waiting_before_retry");
        await new Promise((resolve) => setTimeout(resolve, delayMs));
      }
    }
  }
  return undefined; // Fall back to Baileys default if all attempts fail
}

// ── WhatsApp Connection ───────────────────────────────────────────────────────
async function connectToWhatsApp() {
  if (!isBaileysConnectEnabled()) {
    log.info({ connection_enabled: false }, "baileys_connection_disabled");
    return;
  }

  const { state, saveCreds } = await useMultiFileAuthState(SESSIONS_DIR);

  // Fetch latest WA Web version (with timeout + retry for VPN resilience)
  const version = await fetchVersionWithRetry(3, 10000);

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
      currentQR = qr;
      qrTimestamp = Date.now();
      log.info({ event_type: "connection.update", qr_present: true }, "qr_code_generated");
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
      currentQR = null;   // QR no longer needed once connected
      log.info({
        event_type: "connection.update",
        jid_domain: jidDomain(sock.user?.id),
        connected: true,
      }, "whatsapp_connected");
    }
  });

  sock.ev.on("creds.update", saveCreds);

  // ── Inbound Message Handler ─────────────────────────────────────────────────
  sock.ev.on("messages.upsert", (event) => handleMessagesUpsertEvent(event));
}

function startBridge({
  listen = () => app.listen(PORT, () => {
    log.info({ port: PORT }, "baileys_bridge_listening");
  }),
  connect = connectToWhatsApp,
  exit = (code) => process.exit(code),
} = {}) {
  listen();

  if (!isBaileysConnectEnabled()) {
    log.info({ connection_enabled: false }, "baileys_connection_disabled");
    return Promise.resolve(false);
  }

  return Promise.resolve().then(connect).then(() => true).catch((err) => {
    log.error({ error_type: err?.name || "initialization_failed" }, "fatal_baileys_init_error");
    exit(1);
    return false;
  });
}

if (require.main === module) {
  startBridge();
}

function setSocketForTests(nextSocket) {
  sock = nextSocket;
  isReconnecting = false;
}

module.exports = {
  app,
  connectToWhatsApp,
  handleInboundUpsert,
  handleMessagesUpsertEvent,
  handleSend,
  isBaileysConnectEnabled,
  jidDomain,
  normalizePnJid,
  resolveInboundIdentity,
  setSocketForTests,
  startBridge,
};
