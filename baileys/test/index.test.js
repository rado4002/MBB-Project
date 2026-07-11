"use strict";

const assert = require("node:assert/strict");
const { test, after } = require("node:test");
const Module = require("node:module");

const logs = [];
const routes = { get: {}, post: {} };
const dependencyCalls = { auth: 0, version: 0, socket: 0, saveCreds: 0 };
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
    get(path, handler) { routes.get[path] = handler; },
    post(path, handler) { routes.post[path] = handler; },
    listen() {},
  };
}

fakeExpress.json = () => () => {};

Module._load = function load(request, parent, isMain) {
  if (request === "@whiskeysockets/baileys") {
    return {
      DisconnectReason: { loggedOut: 401 },
      fetchLatestWaWebVersion: async () => {
        dependencyCalls.version += 1;
        return { version: [1, 1, 1] };
      },
      makeWASocket: () => {
        dependencyCalls.socket += 1;
        return { ev: { on() {} } };
      },
      useMultiFileAuthState: async () => {
        dependencyCalls.auth += 1;
        return {
          state: {},
          saveCreds() { dependencyCalls.saveCreds += 1; },
        };
      },
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
  BAILEYS_CONNECT_ENABLED: process.env.BAILEYS_CONNECT_ENABLED,
};

process.env.BAILEYS_WEBHOOK_SECRET = "test-webhook-secret";
process.env.FASTAPI_WEBHOOK_URL = "http://test.invalid/webhook";
process.env.WHATSAPP_SEND_ENABLED = "false";
process.env.BAILEYS_CONNECT_ENABLED = "false";
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

function response() {
  return {
    statusCode: 200,
    body: null,
    headers: {},
    status(code) { this.statusCode = code; return this; },
    json(body) { this.body = body; return this; },
    send(body) { this.body = body; return this; },
    setHeader(name, value) { this.headers[name] = value; },
  };
}

test("disabled startup starts HTTP only and touches no connection dependency", async () => {
  let listenCalls = 0;
  let connectCalls = 0;
  let timerCalls = 0;
  const originalSetTimeout = global.setTimeout;
  global.setTimeout = () => { timerCalls += 1; };
  try {
    const started = await bridge.startBridge({
      listen: () => { listenCalls += 1; },
      connect: async () => { connectCalls += 1; },
      exit: () => assert.fail("disabled startup must not exit"),
    });
    assert.equal(started, false);
  } finally {
    global.setTimeout = originalSetTimeout;
  }

  assert.equal(listenCalls, 1);
  assert.equal(connectCalls, 0);
  assert.equal(timerCalls, 0);
  assert.deepEqual(dependencyCalls, { auth: 0, version: 0, socket: 0, saveCreds: 0 });
});

test("disabled health reports local health without WhatsApp readiness", () => {
  const res = response();
  routes.get["/health"]({}, res);
  assert.deepEqual(res.body, {
    status: "ok",
    connection_enabled: false,
    connected: false,
    jid: null,
  });
});

test("disabled QR endpoints expose no QR or identity", async () => {
  const jsonRes = response();
  await routes.get["/qr.json"]({}, jsonRes);
  assert.deepEqual(jsonRes.body, {
    connection_enabled: false,
    connected: false,
    jid: null,
    ts: 0,
    qrDataUrl: null,
  });

  const pageRes = response();
  routes.get["/qr"]({}, pageRes);
  assert.match(pageRes.body, /connection is disabled/i);
  assert.equal(pageRes.body.includes("/qr.json"), false);
});

test("disabled logout touches no socket, session files, or reconnect timer", async () => {
  const fs = require("fs");
  let logoutCalls = 0;
  let rmCalls = 0;
  let mkdirCalls = 0;
  let timerCalls = 0;
  bridge.setSocketForTests({
    user: { id: "redacted@s.whatsapp.net" },
    logout: async () => { logoutCalls += 1; },
  });
  const originals = { rmSync: fs.rmSync, mkdirSync: fs.mkdirSync, setTimeout: global.setTimeout };
  fs.rmSync = () => { rmCalls += 1; };
  fs.mkdirSync = () => { mkdirCalls += 1; };
  global.setTimeout = () => { timerCalls += 1; };
  const res = response();
  try {
    await routes.post["/logout"]({}, res);
  } finally {
    fs.rmSync = originals.rmSync;
    fs.mkdirSync = originals.mkdirSync;
    global.setTimeout = originals.setTimeout;
  }

  assert.deepEqual(res.body, {
    success: false,
    skipped: true,
    reason: "baileys_connect_disabled",
  });
  assert.equal(logoutCalls, 0);
  assert.equal(rmCalls, 0);
  assert.equal(mkdirCalls, 0);
  assert.equal(timerCalls, 0);
});

test("disabled connection drops inbound before backend webhook", async () => {
  let webhookCalls = 0;
  await bridge.handleInboundUpsert({
    type: "notify",
    messages: [message("243812345678@s.whatsapp.net")],
  }, async () => { webhookCalls += 1; });
  assert.equal(webhookCalls, 0);
});

test("disabled connection independently blocks sendMessage", async () => {
  process.env.WHATSAPP_SEND_ENABLED = "true";
  let sendCalls = 0;
  bridge.setSocketForTests({
    user: { id: "redacted@s.whatsapp.net" },
    sendMessage: async () => { sendCalls += 1; },
  });
  const res = response();
  try {
    await bridge.handleSend({ body: { phone: "+243000000000", message: "test" } }, res);
  } finally {
    process.env.WHATSAPP_SEND_ENABLED = "false";
  }
  assert.equal(sendCalls, 0);
  assert.deepEqual(res.body, {
    success: false,
    skipped: true,
    reason: "baileys_connect_disabled",
  });
});

test("default-enabled startup preserves callable connection behavior", async () => {
  delete process.env.BAILEYS_CONNECT_ENABLED;
  let listenCalls = 0;
  let connectCalls = 0;
  const before = { ...dependencyCalls };
  try {
    assert.equal(bridge.isBaileysConnectEnabled(), true);
    const started = await bridge.startBridge({
      listen: () => { listenCalls += 1; },
      connect: async () => { connectCalls += 1; },
      exit: () => assert.fail("mock enabled startup must not exit"),
    });
    assert.equal(started, true);
    await bridge.connectToWhatsApp();
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
  assert.equal(listenCalls, 1);
  assert.equal(connectCalls, 1);
  assert.equal(dependencyCalls.auth, before.auth + 1);
  assert.equal(dependencyCalls.version, before.version + 1);
  assert.equal(dependencyCalls.socket, before.socket + 1);
  assert.equal(dependencyCalls.saveCreds, before.saveCreds);
});

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
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    await bridge.handleInboundUpsert({
      type: "notify",
      messages: [
        message("123456789@lid"),
        message("120363@g.us", { senderPn: "244923456789@s.whatsapp.net" }),
        message("status@broadcast", { senderPn: "244923456789@s.whatsapp.net" }),
        message("244923456789@s.whatsapp.net", { fromMe: true }),
      ],
    }, async () => { webhookCalls += 1; });
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }

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

test("malformed upsert events are skipped without webhook calls", async () => {
  const cases = [
    null,
    42,
    "invalid",
    {},
    { type: "notify" },
    { type: "notify", messages: {} },
    { type: "notify", messages: [] },
  ];
  let webhookCalls = 0;
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    for (const event of cases) {
      await assert.doesNotReject(() => bridge.handleInboundUpsert(
        event,
        async () => { webhookCalls += 1; }
      ));
    }
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
  assert.equal(webhookCalls, 0);
});

test("malformed and unsupported messages are skipped locally", async () => {
  const validBase = message("243812345678@s.whatsapp.net");
  const cases = [
    null,
    { ...validBase, key: undefined },
    { ...validBase, key: { remoteJid: validBase.key.remoteJid } },
    { ...validBase, key: { ...validBase.key, id: "" } },
    message(undefined),
    message("123456789@lid"),
    { ...validBase, message: undefined },
    { ...validBase, message: { audioMessage: {} } },
    { ...validBase, message: { imageMessage: {} } },
    { ...validBase, message: { protocolMessage: {} } },
    { ...validBase, message: { conversation: "" } },
    { ...validBase, message: { extendedTextMessage: { text: "   " } } },
    { ...validBase, messageTimestamp: undefined },
    { ...validBase, messageTimestamp: "not-a-timestamp" },
    { ...validBase, messageTimestamp: Number.MAX_VALUE },
  ];
  let webhookCalls = 0;
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    for (const malformed of cases) {
      await assert.doesNotReject(() => bridge.handleInboundUpsert({
        type: "notify",
        messages: [malformed],
      }, async () => { webhookCalls += 1; }));
    }
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
  assert.equal(webhookCalls, 0);
});

test("mixed batch skips malformed entries and forwards valid plain text once", async () => {
  const calls = [];
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    await bridge.handleInboundUpsert({
      type: "notify",
      messages: [
        null,
        { key: {}, message: { conversation: "secret malformed text" } },
        message("243812345678@s.whatsapp.net"),
        { ...message("243812345678@s.whatsapp.net"), message: { audioMessage: {} } },
      ],
    }, async (...args) => { calls.push(args); });
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }

  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].customer_phone, "+243812345678");
  assert.equal(calls[0][1].whatsapp_message_id, "message-id");
  assert.equal(calls[0][1].content, "private message text");
  assert.equal(calls[0][1].content_type, "text");
  assert.equal(calls[0][1].timestamp, "2024-03-09T16:00:00.000Z");
});

test("extended text remains supported", async () => {
  const calls = [];
  const extended = message("243812345678@s.whatsapp.net");
  extended.message = { extendedTextMessage: { text: "supported extended text" } };
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    await bridge.handleInboundUpsert({ type: "notify", messages: [extended] },
      async (...args) => { calls.push(args); });
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].content, "supported extended text");
});

test("listener wrapper catches rejected handler promises with redacted logs", async () => {
  const firstLog = logs.length;
  bridge.handleMessagesUpsertEvent(
    { privatePayload: "listener-sensitive-payload" },
    async () => { throw new Error("listener-sensitive-error"); }
  );
  await new Promise((resolve) => setImmediate(resolve));

  const serializedLogs = JSON.stringify(logs.slice(firstLog));
  assert.match(serializedLogs, /inbound_handler_rejected/);
  assert.equal(serializedLogs.includes("listener-sensitive-payload"), false);
  assert.equal(serializedLogs.includes("listener-sensitive-error"), false);
});

test("malformed inbound diagnostics contain no sensitive values", async () => {
  const firstLog = logs.length;
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    await bridge.handleInboundUpsert({
      type: "notify",
      messages: [{
        key: { remoteJid: "243899999999@s.whatsapp.net", id: "" },
        message: { conversation: "sensitive malformed content" },
        messageTimestamp: 1710000000,
      }],
    }, async () => assert.fail("malformed message must not reach webhook"));
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }

  const serializedLogs = JSON.stringify(logs.slice(firstLog));
  assert.equal(serializedLogs.includes("243899999999"), false);
  assert.equal(serializedLogs.includes("sensitive malformed content"), false);
  assert.equal(serializedLogs.includes("@s.whatsapp.net"), false);
  assert.equal(serializedLogs.includes("test-webhook-secret"), false);
  assert.match(serializedLogs, /invalid_message_id/);
});

test("forwards an authoritative international identity unchanged", async () => {
  const calls = [];
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    await bridge.handleInboundUpsert({
      type: "notify",
      messages: [message("987654321@lid", {
        senderPn: "244923456789@s.whatsapp.net",
      })],
    }, async (...args) => { calls.push(args); });
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }

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
