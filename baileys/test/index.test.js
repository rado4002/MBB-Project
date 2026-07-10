"use strict";

const assert = require("node:assert/strict");
const { test, after } = require("node:test");
const Module = require("node:module");

const logs = [];
const originalLoad = Module._load;

function fakeLogger() {
  return {
    info: (...args) => logs.push(["info", ...args]),
    warn: (...args) => logs.push(["warn", ...args]),
    error: (...args) => logs.push(["error", ...args]),
  };
}

function fakeExpress() {
  return {
    use() {},
    get() {},
    post() {},
    listen() {},
  };
}

fakeExpress.json = () => () => {};

Module._load = function load(request, parent, isMain) {
  if (request === "@whiskeysockets/baileys") {
    return {
      DisconnectReason: { loggedOut: 401 },
      fetchLatestWaWebVersion: async () => ({ version: [1, 1, 1] }),
      makeWASocket: () => ({}),
      useMultiFileAuthState: async () => ({ state: {}, saveCreds() {} }),
    };
  }
  if (request === "axios") return { post: async () => ({ status: 200 }) };
  if (request === "express") return fakeExpress;
  if (request === "pino") return () => fakeLogger();
  return originalLoad(request, parent, isMain);
};

const originalEnvironment = {
  BAILEYS_WEBHOOK_SECRET: process.env.BAILEYS_WEBHOOK_SECRET,
  FASTAPI_WEBHOOK_URL: process.env.FASTAPI_WEBHOOK_URL,
  WHATSAPP_SEND_ENABLED: process.env.WHATSAPP_SEND_ENABLED,
};

process.env.BAILEYS_WEBHOOK_SECRET = "test-webhook-secret";
process.env.FASTAPI_WEBHOOK_URL = "http://test.invalid/webhook";
process.env.WHATSAPP_SEND_ENABLED = "false";
const bridge = require("../src/index.js");

after(() => {
  Module._load = originalLoad;
  for (const [name, value] of Object.entries(originalEnvironment)) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
});

function message(remoteJid, fields = {}) {
  return {
    key: { remoteJid, id: "message-id", ...fields },
    message: { conversation: "private message text" },
    messageTimestamp: 1710000000,
  };
}

test("normalizes direct international PN identities", () => {
  for (const [phone, expected] of [
    ["243812345678@s.whatsapp.net", "+243812345678"],
    ["+244923456789@s.whatsapp.net", "+244923456789"],
    ["250788123456@s.whatsapp.net", "+250788123456"],
    ["+33142345678@s.whatsapp.net", "+33142345678"],
  ]) {
    const result = bridge.resolveInboundIdentity(message(phone));
    assert.deepEqual(result, {
      phone: expected,
      source: "remoteJid",
      jidDomain: "s.whatsapp.net",
    });
  }
});

test("resolves LID events from authoritative senderPn or participantPn", () => {
  assert.equal(
    bridge.resolveInboundIdentity(message("123456789@lid", {
      senderPn: "244923456789@s.whatsapp.net",
    })).phone,
    "+244923456789"
  );

  const participant = bridge.resolveInboundIdentity(message("123456789@lid", {
    participant: "123456789@lid",
    participantPn: "+250788123456@s.whatsapp.net",
  }));
  assert.deepEqual(participant, {
    phone: "+250788123456",
    source: "participantPn",
    jidDomain: "lid",
  });

  const direct = bridge.resolveInboundIdentity(message("243812345678@s.whatsapp.net", {
    participantPn: "244923456789@s.whatsapp.net",
  }));
  assert.equal(direct.phone, "+243812345678");
  assert.equal(direct.source, "remoteJid");
});

test("skips unresolved, group, status, broadcast, fromMe, and invalid identities", () => {
  assert.equal(bridge.resolveInboundIdentity(message("123456789@lid")).skipReason, "unresolved_lid");
  assert.equal(bridge.resolveInboundIdentity(message("120363@g.us")).skipReason, "group_message");
  assert.equal(bridge.resolveInboundIdentity(message("status@broadcast")).skipReason, "status_or_broadcast");
  assert.equal(bridge.resolveInboundIdentity(message("243812345678@s.whatsapp.net", { fromMe: true })).skipReason, "from_me");
  assert.equal(bridge.resolveInboundIdentity(message("123@newsletter")).skipReason, "unsupported_jid_domain");
  assert.equal(bridge.resolveInboundIdentity(message("not-a-phone@s.whatsapp.net")).skipReason, "invalid_phone_identity");
});

test("never forwards unresolved LID and logs only redacted diagnostics", async () => {
  const firstLog = logs.length;
  let webhookCalls = 0;
  await bridge.handleInboundUpsert({
    type: "notify",
    messages: [
      message("123456789@lid"),
      message("120363@g.us", { senderPn: "244923456789@s.whatsapp.net" }),
      message("status@broadcast", { senderPn: "244923456789@s.whatsapp.net" }),
      message("244923456789@s.whatsapp.net", { fromMe: true }),
    ],
  }, async () => { webhookCalls += 1; });

  assert.equal(webhookCalls, 0);
  const serializedLogs = JSON.stringify(logs.slice(firstLog));
  assert.equal(serializedLogs.includes("123456789"), false);
  assert.equal(serializedLogs.includes("private message text"), false);
  assert.equal(serializedLogs.includes("message-id"), false);
  assert.equal(serializedLogs.includes("test-webhook-secret"), false);
  assert.match(serializedLogs, /unresolved_lid/);
  assert.match(serializedLogs, /group_message/);
  assert.match(serializedLogs, /status_or_broadcast/);
  assert.match(serializedLogs, /from_me/);
});

test("forwards an authoritative international identity unchanged", async () => {
  const calls = [];
  await bridge.handleInboundUpsert({
    type: "notify",
    messages: [message("987654321@lid", {
      senderPn: "244923456789@s.whatsapp.net",
    })],
  }, async (...args) => { calls.push(args); });

  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].customer_phone, "+244923456789");
  assert.equal(calls[0][1].whatsapp_message_id, "message-id");
});

test("outbound disabled mode makes zero sendMessage calls and logs no sensitive values", async () => {
  let sendCalls = 0;
  bridge.setSocketForTests({
    user: { id: "244923456789@s.whatsapp.net" },
    sendMessage: async () => { sendCalls += 1; },
  });

  const response = {
    statusCode: null,
    body: null,
    status(code) {
      this.statusCode = code;
      return this;
    },
    json(body) {
      this.body = body;
      return this;
    },
  };

  await bridge.handleSend({
    body: { phone: "+244923456789", message: "private message text" },
  }, response);

  assert.equal(sendCalls, 0);
  assert.equal(response.statusCode, 200);
  assert.deepEqual(response.body, {
    success: false,
    skipped: true,
    reason: "whatsapp_send_disabled",
  });

  const serializedLogs = JSON.stringify(logs);
  assert.equal(serializedLogs.includes("244923456789"), false);
  assert.equal(serializedLogs.includes("987654321"), false);
  assert.equal(serializedLogs.includes("private message text"), false);
  assert.equal(serializedLogs.includes("message-id"), false);
  assert.equal(serializedLogs.includes("@s.whatsapp.net"), false);
  assert.equal(serializedLogs.includes("test-webhook-secret"), false);
});
