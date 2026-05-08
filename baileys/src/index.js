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
const qrcode = require("qrcode-terminal");

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
      connected: sock?.user != null,
      jid: sock?.user?.id ?? null,
    },
  });
});

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
  if (!sock?.user || isReconnecting) {
    return res.status(503).json({ error: "WhatsApp not connected" });
  }

  try {
    const normalized = phone.replace(/^\+/, "");  // strip leading + for JID
    const jid = `${normalized}@s.whatsapp.net`;
    await sock.sendMessage(jid, { text: message });
    log.info({ phone, chars: message.length }, "message_sent");
    res.json({ success: true });
  } catch (err) {
    log.error({ phone, err: err.message }, "send_failed");
    res.status(500).json({ error: err.message });
  }
});

// QR status — polled by the dashboard SPA every 10 s
app.get("/qr.json", async (req, res) => {
  const payload = {
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
      log.warn({ err: e.message }, "qr_dataurl_gen_failed");
    }
  }

  res.json(payload);
});

// Live QR dashboard — open http://localhost:3000/qr in a browser
app.get("/qr", (req, res) => {
  res.setHeader("Content-Type", "text/html; charset=utf-8");
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
      log.info({ dir: SESSIONS_DIR }, "session_dir_wiped");
    } catch (wipeErr) {
      log.warn({ err: wipeErr.message }, "session_wipe_failed");
    }

    // Trigger reconnect — new QR will appear on /qr shortly
    setTimeout(() => {
      connectToWhatsApp()
        .catch((err) => log.error({ err: err.message }, "post_logout_reconnect_failed"))
        .finally(() => { isReconnecting = false; });
    }, 1000);

    log.info("user_logged_out");
    res.json({
      success: true,
      message: "Logged out. New QR available at /qr in a few seconds.",
    });
  } catch (err) {
    log.error({ err: err.message }, "logout_failed");
    res.status(500).json({ error: err.message });
  }
});

app.listen(PORT, () => {
  log.info({ port: PORT }, "baileys_bridge_listening");
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
        { attempt, max_attempts: maxAttempts, err: err.message },
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
      log.info("qr_code_generated — open http://localhost:3000/qr or scan from terminal");
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
      currentQR = null;   // QR no longer needed once connected
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
        customer_phone: `+${phone}`,        // FastAPI expects E.164 with +
        whatsapp_message_id: msg.key.id,    // FastAPI expects this field name
        content: text ?? "",
        content_type: msgType,              // FastAPI expects content_type not type
        timestamp: new Date(Number(msg.messageTimestamp) * 1000).toISOString(),
      };

      log.info({ phone, type: msgType, wa_id: msg.key.id }, "inbound_message");

      const headers = { "Content-Type": "application/json" };
      if (WEBHOOK_SECRET) headers["X-Webhook-Secret"] = WEBHOOK_SECRET;

      // Forward to FastAPI with retry (up to 3 attempts — DRC network resilience)
      let attempt = 0;
      while (attempt < 3) {
        try {
          await axios.post(FASTAPI_WEBHOOK_URL, payload, {
            timeout: 8000,
            headers,
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
            log.error({ phone, wa_id: payload.whatsapp_message_id }, "fastapi_forward_exhausted");
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
