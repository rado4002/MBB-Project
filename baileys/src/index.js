"use strict";

const {
  makeWASocket,
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestWaWebVersion,
} = require("@whiskeysockets/baileys");
const axios = require("axios");
const crypto = require("crypto");
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

function isQrEndpointsEnabled() {
  return String(process.env.BAILEYS_QR_ENDPOINTS_ENABLED || "false").toLowerCase() === "true";
}

function isLogoutEnabled() {
  return String(process.env.BAILEYS_LOGOUT_ENABLED || "false").toLowerCase() === "true";
}

function recoveryToken() {
  return String(process.env.BAILEYS_RECOVERY_TOKEN || "");
}

function requestHeader(req, name) {
  if (typeof req?.get === "function") return req.get(name);
  const headers = req?.headers || {};
  return headers[name.toLowerCase()] || headers[name];
}

function hasValidRecoveryAuth(req) {
  const token = recoveryToken();
  if (!token) return false;
  const authorization = requestHeader(req, "authorization");
  if (typeof authorization !== "string" || !authorization.startsWith("Basic ")) return false;
  try {
    const supplied = Buffer.from(authorization.slice(6), "base64");
    const expected = Buffer.from(`recovery:${token}`, "utf8");
    return supplied.length === expected.length && crypto.timingSafeEqual(supplied, expected);
  } catch {
    return false;
  }
}

function requireRecoveryAuth(req, res, routeCategory = null) {
  if (!recoveryToken()) {
    res.status(503).json({ error: "recovery_auth_unconfigured" });
    return false;
  }
  if (!hasValidRecoveryAuth(req)) {
    if (routeCategory === "qr") {
      log.warn({
        route_category: "qr",
        reason: "missing_or_invalid_auth",
      }, "baileys.protected_route_rejected");
    }
    res.setHeader("WWW-Authenticate", 'Basic realm="Baileys recovery"');
    res.status(401).json({ error: "recovery_auth_required" });
    return false;
  }
  return true;
}

function safeStatusCategory(error) {
  const status = error?.response?.status;
  return Number.isInteger(status) ? Math.floor(status / 100) * 100 : "unknown";
}

function safeForwardFailureCategory(error) {
  const status = error?.response?.status;
  if (Number.isInteger(status) && status >= 400 && status < 500) return "http_4xx";
  if (Number.isInteger(status) && status >= 500 && status < 600) return "http_5xx";

  const code = error?.code;
  if (code === "ECONNABORTED" || code === "ETIMEDOUT") return "timeout";
  if (["ECONNREFUSED", "ECONNRESET", "ENOTFOUND", "EAI_AGAIN"].includes(code)) {
    return "connection";
  }
  return "unknown";
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

function normalizedMessageTimestamp(value) {
  if (value === null || value === undefined) return null;

  let seconds;
  try {
    if (typeof value === "object") {
      if (typeof value.toNumber !== "function") return null;
      seconds = value.toNumber();
    } else {
      seconds = Number(value);
    }
  } catch {
    return null;
  }

  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  const timestampMs = seconds * 1000;
  if (!Number.isFinite(timestampMs) || timestampMs <= 0) return null;

  try {
    const date = new Date(timestampMs);
    if (!Number.isFinite(date.getTime())) return null;
    return date.toISOString();
  } catch {
    return null;
  }
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
  if (!currentSocket?.user || connectPromise) {
    return res.status(503).json({ error: "WhatsApp not connected" });
  }

  const normalized = normalizePhoneInput(phone);
  if (!normalized) {
    return res.status(400).json({ error: "phone must be an international number" });
  }

  try {
    const jid = `${normalized.slice(1)}@s.whatsapp.net`;
    await currentSocket.sendMessage(jid, { text: message });
    log.info({ content_type: "text", message_present: true }, "message_sent");
    return res.json({ success: true });
  } catch (err) {
    log.error({ error_type: safeErrorType(err), status_category: safeStatusCategory(err) }, "send_failed");
    return res.status(500).json({ error: "send failed" });
  }
}

async function handleInboundUpsert(event, postWebhook = axios.post, context = {}) {
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
  const eventSocketGeneration = Number.isInteger(context.socketGeneration)
    ? context.socketGeneration
    : socketGeneration;

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
      if (msg.messageStubType !== undefined && msg.messageStubType !== null) {
        logInvalidInbound("message_stub");
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

      const timestamp = normalizedMessageTimestamp(msg.messageTimestamp);
      if (timestamp === null) {
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
        candidate_eligible: true,
        socket_generation: eventSocketGeneration,
        upsert_type: type,
      }, "inbound_message");

      const headers = { "Content-Type": "application/json" };
      if (WEBHOOK_SECRET) headers["X-Webhook-Secret"] = WEBHOOK_SECRET;

      const maximumAttempts = 3;
      for (let attemptNumber = 1; attemptNumber <= maximumAttempts; attemptNumber += 1) {
        log.info({
          attempt_number: attemptNumber,
          maximum_attempts: maximumAttempts,
          socket_generation: eventSocketGeneration,
          upsert_type: type,
        }, "fastapi_forward_attempted");
        try {
          const response = await postWebhook(FASTAPI_WEBHOOK_URL, payload, {
            timeout: 8000,
            headers,
          });
          log.info({
            successful_attempt_number: attemptNumber,
            socket_generation: eventSocketGeneration,
            http_status: Number.isInteger(response?.status) ? response.status : "unknown",
          }, "fastapi_forward_succeeded");
          break;
        } catch (err) {
          const failureCategory = safeForwardFailureCategory(err);
          const willRetry = attemptNumber < maximumAttempts;
          log.warn({
            attempt_number: attemptNumber,
            maximum_attempts: maximumAttempts,
            will_retry: willRetry,
            failure_category: failureCategory,
            socket_generation: eventSocketGeneration,
          }, "fastapi_forward_failed");
          if (willRetry) {
            await new Promise((resolve) => setTimeout(resolve, 2000 * attemptNumber));
          } else {
            log.error({
              attempts_made: maximumAttempts,
              socket_generation: eventSocketGeneration,
              failure_category: failureCategory,
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

function handleMessagesUpsertEvent(event, handler = handleInboundUpsert, context = {}) {
  void Promise.resolve()
    .then(() => handler(event, undefined, context))
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

let currentSocket = null;
let currentQR = null;   // latest QR string from connection.update, cleared on connect
let qrTimestamp = 0;    // epoch ms of last QR update — used by polling endpoint
let connectPromise = null;
let reconnectTimer = null;
let reconnectAttempt = 0;
let stopping = false;
let loggedOut = false;
let socketGeneration = 0;
let httpServer = null;

const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const RECONNECT_JITTER_MS = 250;

const lifecycleDeps = {
  loadAuth: useMultiFileAuthState,
  fetchVersion: () => fetchVersionWithRetry(3, 10000),
  makeSocket: makeWASocket,
  random: Math.random,
  setTimer: setTimeout,
  clearTimer: clearTimeout,
};

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
      send: "POST /send",
    },
    whatsapp: {
      connection_enabled: connectionEnabled,
      connected: connectionEnabled && currentSocket?.user != null,
      connection: connectionEnabled && currentSocket?.user != null ? "connected" : "disconnected",
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
    connected: connectionEnabled && currentSocket?.user != null,
    connection: connectionEnabled && currentSocket?.user != null ? "connected" : "disconnected",
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
  if (!isQrEndpointsEnabled()) return res.status(404).json({ error: "qr_endpoints_disabled" });
  if (!requireRecoveryAuth(req, res, "qr")) return undefined;
  const connectionEnabled = isBaileysConnectEnabled();

  const payload = {
    connection_enabled: connectionEnabled,
    connected: connectionEnabled && currentSocket?.user != null,
    ts: connectionEnabled ? qrTimestamp : 0,
    qrDataUrl: null,
  };

  if (connectionEnabled && currentQR) {
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
  if (!isQrEndpointsEnabled()) return res.status(404).json({ error: "qr_endpoints_disabled" });
  if (!requireRecoveryAuth(req, res, "qr")) return undefined;
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
  if (!isLogoutEnabled()) {
    log.warn({ action: "logout", reason: "disabled" }, "baileys.logout_disabled");
    return res.status(404).json({ error: "logout_disabled" });
  }
  if (!requireRecoveryAuth(req, res)) return undefined;
  if (!isBaileysConnectEnabled()) {
    return res.status(200).json({
      success: false,
      skipped: true,
      reason: "baileys_connect_disabled",
    });
  }

  if (!currentSocket && !connectPromise) {
    return res.status(400).json({ error: "No active WhatsApp connection" });
  }

  try {
    await logoutCurrentSocket();

    // Wipe session files so next connection prompts a fresh QR
    try {
      fs.rmSync(SESSIONS_DIR, { recursive: true, force: true });
      fs.mkdirSync(SESSIONS_DIR, { recursive: true });
      log.info({ session_reset: true }, "session_dir_wiped");
    } catch (wipeErr) {
      log.warn({ error_type: safeErrorType(wipeErr) }, "session_wipe_failed");
    }

    log.info("user_logged_out");
    res.json({
      success: true,
      message: "Logged out. Automatic reconnect is disabled.",
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

// ── WhatsApp Connection Lifecycle ─────────────────────────────────────────────
function clearReconnectTimer() {
  if (reconnectTimer !== null) {
    lifecycleDeps.clearTimer(reconnectTimer);
    reconnectTimer = null;
  }
}

function reconnectDelayMs(attempt) {
  const exponential = RECONNECT_MIN_MS * (2 ** Math.max(0, attempt - 1));
  const jitter = Math.floor(lifecycleDeps.random() * RECONNECT_JITTER_MS);
  return Math.min(RECONNECT_MAX_MS, exponential + jitter);
}

function disconnectCategory(code) {
  if (!Number.isInteger(code)) return "unknown";
  const mappings = [
    ["loggedOut", "logged_out"],
    ["restartRequired", "restart_required"],
    ["connectionClosed", "connection_closed"],
    ["connectionLost", "connection_lost"],
    ["timedOut", "timed_out"],
    ["badSession", "bad_session"],
  ];
  for (const [reason, category] of mappings) {
    if (Number.isInteger(DisconnectReason[reason]) && code === DisconnectReason[reason]) {
      return category;
    }
  }
  return "recoverable_close";
}

function reconnectSkipReason() {
  if (!isBaileysConnectEnabled()) return "connection_disabled";
  if (stopping) return "stopping";
  if (loggedOut) return "logged_out";
  if (connectPromise) return "connect_in_progress";
  if (reconnectTimer !== null) return "timer_already_scheduled";
  return null;
}

function detachSocket(socket) {
  try {
    socket?.ev?.removeAllListeners?.("connection.update");
    socket?.ev?.removeAllListeners?.("creds.update");
    socket?.ev?.removeAllListeners?.("messages.upsert");
  } catch (err) {
    log.warn({ error_type: safeErrorType(err) }, "baileys.socket_detach_failed");
  }
}

function closeSocketSafely(socket) {
  try {
    socket?.end?.();
  } catch (err) {
    log.warn({ error_type: safeErrorType(err) }, "baileys.socket_close_failed");
  }
}

function replaceCurrentSocket(nextSocket) {
  const previous = currentSocket;
  if (previous && previous !== nextSocket) {
    detachSocket(previous);
    closeSocketSafely(previous);
    log.info({ replaced: true }, "baileys.socket_replaced");
  }
  currentSocket = nextSocket;
  socketGeneration += 1;
  return socketGeneration;
}

function scheduleReconnect(category = "recoverable_close", generation = socketGeneration) {
  const skipReason = reconnectSkipReason();
  if (skipReason) {
    log.info({
      skip_reason: skipReason,
      socket_generation: generation,
      disconnect_category: category,
    }, "baileys.reconnect_skipped");
    return false;
  }

  reconnectAttempt += 1;
  const delay = reconnectDelayMs(reconnectAttempt);
  reconnectTimer = lifecycleDeps.setTimer(() => {
    reconnectTimer = null;
    if (stopping || loggedOut || !isBaileysConnectEnabled()) return;
    void connectToWhatsApp("scheduled_reconnect").catch((err) => {
      log.error({
        error_type: safeErrorType(err),
        socket_generation: generation,
        disconnect_category: "unknown",
      }, "baileys.reconnect_failed");
      scheduleReconnect("unknown", socketGeneration);
    });
  }, delay);
  log.info({
    reconnect_attempt: reconnectAttempt,
    delay_ms: delay,
    socket_generation: generation,
    disconnect_category: category,
  }, "baileys.reconnect_scheduled");
  return true;
}

function handleConnectionUpdate(socket, generation, update = {}) {
  if (socket !== currentSocket || generation !== socketGeneration) {
    log.info({
      skip_reason: "stale_generation",
      socket_generation: generation,
      authoritative_socket_generation: socketGeneration,
      disconnect_category: "unknown",
    }, "baileys.reconnect_skipped");
    return;
  }

  const { connection, lastDisconnect, qr } = update;
  if (qr) {
    currentQR = qr;
    qrTimestamp = Date.now();
    log.info({ event_type: "connection.update", qr_present: true }, "qr_code_generated");
  }

  if (connection === "open") {
    clearReconnectTimer();
    reconnectAttempt = 0;
    currentQR = null;
    qrTimestamp = 0;
    log.info({ connected: true }, "baileys.socket_open");
    return;
  }
  if (connection !== "close") return;

  let code;
  try {
    code = lastDisconnect?.error?.output?.statusCode;
  } catch (err) {
    log.warn({
      error_type: safeErrorType(err),
      socket_generation: generation,
      disconnect_category: "unknown",
    }, "baileys.disconnect_classification_failed");
  }
  const category = disconnectCategory(code);
  const wasLoggedOut = loggedOut || category === "logged_out";
  currentSocket = null;
  currentQR = null;
  qrTimestamp = 0;
  if (wasLoggedOut) {
    loggedOut = true;
    clearReconnectTimer();
    reconnectAttempt = 0;
    log.info({
      skip_reason: "logged_out",
      socket_generation: generation,
      disconnect_category: "logged_out",
    }, "baileys.reconnect_skipped");
    return;
  }
  if (stopping) {
    log.info({
      skip_reason: "stopping",
      socket_generation: generation,
      disconnect_category: category,
    }, "baileys.reconnect_skipped");
    return;
  }
  scheduleReconnect(category, generation);
}

function connectToWhatsApp(initiatingCategory = "manual_or_startup") {
  if (!isBaileysConnectEnabled() || stopping || loggedOut) {
    clearReconnectTimer();
    log.info({
      skip_reason: reconnectSkipReason() || "unknown",
      socket_generation: socketGeneration,
      disconnect_category: "unknown",
    }, "baileys.reconnect_skipped");
    return Promise.resolve(undefined);
  }
  if (connectPromise) {
    log.info({ reused: true }, "baileys.connect_reused");
    return connectPromise;
  }

  clearReconnectTimer();
  log.info({
    next_socket_generation: socketGeneration + 1,
    reconnect_attempt: reconnectAttempt,
    initiating_category: initiatingCategory,
  }, "baileys.connect_started");
  const attempt = (async () => {
    const { state, saveCreds } = await lifecycleDeps.loadAuth(SESSIONS_DIR);
    const version = await lifecycleDeps.fetchVersion();
    if (stopping || loggedOut || !isBaileysConnectEnabled()) return undefined;
    const nextSocket = lifecycleDeps.makeSocket({
      auth: state,
      logger: pino({ level: process.env.BAILEYS_LOG_LEVEL || "silent" }),
      browser: ["MBB ya Kin", "Chrome", "1.0.0"],
      syncFullHistory: false,
      generateHighQualityLinkPreview: false,
      ...(version && { version }),
    });
    const generation = replaceCurrentSocket(nextSocket);
    nextSocket.ev.on("connection.update", (update) =>
      handleConnectionUpdate(nextSocket, generation, update));
    nextSocket.ev.on("creds.update", (...args) => {
      if (nextSocket === currentSocket && generation === socketGeneration) return saveCreds(...args);
      return undefined;
    });
    nextSocket.ev.on("messages.upsert", (event) => {
      if (nextSocket === currentSocket && generation === socketGeneration) {
        handleMessagesUpsertEvent(event, handleInboundUpsert, { socketGeneration: generation });
      }
    });
    return nextSocket;
  })();
  connectPromise = attempt;
  attempt.then(
    () => { if (connectPromise === attempt) connectPromise = null; },
    () => { if (connectPromise === attempt) connectPromise = null; }
  );
  return attempt;
}

async function logoutCurrentSocket() {
  loggedOut = true;
  clearReconnectTimer();
  reconnectAttempt = 0;
  const socket = currentSocket;
  currentSocket = null;
  currentQR = null;
  qrTimestamp = 0;
  detachSocket(socket);
  if (socket?.user && typeof socket.logout === "function") await socket.logout();
}

async function shutdownBridge(server = httpServer) {
  if (stopping) return;
  stopping = true;
  log.info({ stopping: true }, "baileys.shutdown_started");
  clearReconnectTimer();
  reconnectAttempt = 0;
  const socket = currentSocket;
  currentSocket = null;
  currentQR = null;
  qrTimestamp = 0;
  detachSocket(socket);
  closeSocketSafely(socket);
  if (server?.close) {
    await new Promise((resolve) => {
      try { server.close(() => resolve()); } catch (err) {
        log.warn({ error_type: safeErrorType(err) }, "baileys.http_close_failed");
        resolve();
      }
    });
  }
  log.info({ stopping: true }, "baileys.shutdown_complete");
}

function startBridge({
  listen = () => app.listen(PORT, () => {
    log.info({ port: PORT }, "baileys_bridge_listening");
  }),
  connect = connectToWhatsApp,
  exit = (code) => process.exit(code),
} = {}) {
  httpServer = listen() || httpServer;

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
  process.once("SIGINT", () => { void shutdownBridge().finally(() => process.exit(0)); });
  process.once("SIGTERM", () => { void shutdownBridge().finally(() => process.exit(0)); });
  void startBridge();
}

function setSocketForTests(nextSocket) {
  currentSocket = nextSocket;
  socketGeneration += 1;
}

function setLifecycleDependenciesForTests(overrides = {}) {
  Object.assign(lifecycleDeps, overrides);
}

function resetLifecycleForTests() {
  clearReconnectTimer();
  currentSocket = null;
  currentQR = null;
  qrTimestamp = 0;
  connectPromise = null;
  reconnectAttempt = 0;
  stopping = false;
  loggedOut = false;
  socketGeneration = 0;
  httpServer = null;
  Object.assign(lifecycleDeps, {
    loadAuth: useMultiFileAuthState,
    fetchVersion: () => fetchVersionWithRetry(3, 10000),
    makeSocket: makeWASocket,
    random: Math.random,
    setTimer: setTimeout,
    clearTimer: clearTimeout,
  });
}

function getLifecycleStateForTests() {
  return {
    currentSocket,
    connectPromise,
    reconnectTimer,
    reconnectAttempt,
    stopping,
    loggedOut,
    socketGeneration,
    currentQR,
  };
}

module.exports = {
  app,
  connectToWhatsApp,
  getLifecycleStateForTests,
  handleConnectionUpdate,
  handleInboundUpsert,
  handleMessagesUpsertEvent,
  handleSend,
  isBaileysConnectEnabled,
  jidDomain,
  normalizePnJid,
  resolveInboundIdentity,
  resetLifecycleForTests,
  scheduleReconnect,
  setLifecycleDependenciesForTests,
  setSocketForTests,
  shutdownBridge,
  startBridge,
};
