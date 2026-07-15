"use strict";

const assert = require("node:assert/strict");
const { test, after } = require("node:test");
const Module = require("node:module");

const logs = [];
const routes = { get: {}, post: {} };
const dependencyCalls = { auth: 0, version: 0, socket: 0, saveCreds: 0 };
const originalLoad = Module._load;

const fakeBaileysModule = {
  DisconnectReason: {
    loggedOut: 401,
    connectionClosed: 428,
    connectionLost: 408,
    connectionReplaced: 440,
    restartRequired: 515,
    timedOut: 408,
    badSession: 500,
    multideviceMismatch: 411,
    unavailableService: 503,
  },
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
  if (request === "axios") return { post: async () => ({ status: 200 }) };
  if (request === "express") return fakeExpress;
  if (request === "pino") return () => fakeLogger();
  if (request === "qrcode") return { toDataURL: async () => "data:image/png;base64,mocked" };
  return originalLoad(request, parent, isMain);
};

const originalEnvironment = {
  BAILEYS_WEBHOOK_SECRET: process.env.BAILEYS_WEBHOOK_SECRET,
  FASTAPI_WEBHOOK_URL: process.env.FASTAPI_WEBHOOK_URL,
  WHATSAPP_SEND_ENABLED: process.env.WHATSAPP_SEND_ENABLED,
  BAILEYS_CONNECT_ENABLED: process.env.BAILEYS_CONNECT_ENABLED,
  BAILEYS_QR_ENDPOINTS_ENABLED: process.env.BAILEYS_QR_ENDPOINTS_ENABLED,
  BAILEYS_LOGOUT_ENABLED: process.env.BAILEYS_LOGOUT_ENABLED,
  BAILEYS_RECOVERY_TOKEN: process.env.BAILEYS_RECOVERY_TOKEN,
};

process.env.BAILEYS_WEBHOOK_SECRET = "test-webhook-secret";
process.env.FASTAPI_WEBHOOK_URL = "http://test.invalid/webhook";
process.env.WHATSAPP_SEND_ENABLED = "false";
process.env.BAILEYS_CONNECT_ENABLED = "false";
const bridge = require("../src/index.js");
bridge.setBaileysModuleForTests(fakeBaileysModule);

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

function recoveryRequest(token = "test-recovery-token") {
  return {
    headers: {
      authorization: `Basic ${Buffer.from(`recovery:${token}`).toString("base64")}`,
    },
  };
}

function auditEventsSince(firstLog, eventName) {
  return logs.slice(firstLog).filter((entry) => entry.includes(eventName));
}

function namedEventsSince(firstLog, eventName) {
  return logs.slice(firstLog).filter((entry) => entry.at(-1) === eventName);
}

async function withImmediateTimeout(action) {
  const originalSetTimeout = global.setTimeout;
  global.setTimeout = (callback) => {
    callback();
    return 1;
  };
  try {
    return await action();
  } finally {
    global.setTimeout = originalSetTimeout;
  }
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
    connection: "disconnected",
  });
});

test("disabled QR endpoints expose no QR or identity", async () => {
  const firstLog = logs.length;
  const jsonRes = response();
  await routes.get["/qr.json"]({}, jsonRes);
  assert.equal(jsonRes.statusCode, 404);
  assert.deepEqual(jsonRes.body, { error: "qr_endpoints_disabled" });

  const pageRes = response();
  routes.get["/qr"]({}, pageRes);
  assert.equal(pageRes.statusCode, 404);
  assert.deepEqual(pageRes.body, { error: "qr_endpoints_disabled" });
  assert.equal(auditEventsSince(firstLog, "baileys.protected_route_rejected").length, 0);
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
    error: "logout_disabled",
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

test("resolves v7 direct LID alternates without logging identity", async () => {
  const firstLog = logs.length;
  const deliveries = [];
  const cases = [
    message("111111111@lid", {
      remoteJidAlt: "243812345678@s.whatsapp.net",
    }),
    message("222222222@lid", {
      participant: "333333333@lid",
      participantAlt: "250788123456@s.whatsapp.net",
    }),
  ];
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    for (const candidate of cases) {
      await bridge.handleInboundUpsert({ type: "notify", messages: [candidate] },
        async (_url, payload) => { deliveries.push(payload.customer_phone); return { status: 200 }; });
    }
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }

  assert.deepEqual(deliveries, ["+243812345678", "+250788123456"]);
  const serialized = JSON.stringify(logs.slice(firstLog));
  for (const sensitive of ["111111111", "222222222", "333333333", "243812345678", "250788123456"]) {
    assert.equal(serialized.includes(sensitive), false);
  }
});

test("rejects invalid v7 PN alternates and conflicting authoritative identities", async () => {
  const firstLog = logs.length;
  const invalidAlternates = [
    "120363@g.us",
    "status@broadcast",
    "list@broadcast",
    "444444444@lid",
    "malformed-alternate",
  ];
  let attempts = 0;
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    for (const remoteJidAlt of invalidAlternates) {
      await bridge.handleInboundUpsert({
        type: "notify",
        messages: [message("111111111@lid", { remoteJidAlt })],
      }, async () => { attempts += 1; return { status: 200 }; });
    }
    await bridge.handleInboundUpsert({
      type: "notify",
      messages: [message("111111111@lid", {
        remoteJidAlt: "243812345678@s.whatsapp.net",
        senderPn: "250788123456@s.whatsapp.net",
      })],
    }, async () => { attempts += 1; return { status: 200 }; });
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }

  assert.equal(attempts, 0);
  assert.equal(namedEventsSince(firstLog, "inbound_message_skipped")
    .some((entry) => entry[1].skip_reason === "identity_conflict"), true);
  const serialized = JSON.stringify(logs.slice(firstLog));
  for (const sensitive of ["111111111", "444444444", "243812345678", "250788123456"]) {
    assert.equal(serialized.includes(sensitive), false);
  }
});

test("allows matching duplicate v7 PN identities", async () => {
  let attempts = 0;
  let deliveredPhone;
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    await bridge.handleInboundUpsert({
      type: "notify",
      messages: [message("111111111@lid", {
        remoteJidAlt: "243812345678@s.whatsapp.net",
        senderPn: "243812345678@s.whatsapp.net",
      })],
    }, async (_url, payload) => {
      attempts += 1;
      deliveredPhone = payload.customer_phone;
      return { status: 200 };
    });
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
  assert.equal(attempts, 1);
  assert.equal(deliveredPhone, "+243812345678");
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

test("append and replace events produce no candidate or forwarding activity", async () => {
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    for (const type of ["append", "replace"]) {
      const firstLog = logs.length;
      let attempts = 0;
      await bridge.handleInboundUpsert({
        type,
        messages: [message("243812345678@s.whatsapp.net")],
      }, async () => { attempts += 1; return { status: 200 }; });
      assert.equal(attempts, 0);
      for (const eventName of [
        "inbound_message",
        "fastapi_forward_attempted",
        "fastapi_forward_failed",
        "fastapi_forward_exhausted",
        "fastapi_forward_succeeded",
      ]) assert.equal(namedEventsSince(firstLog, eventName).length, 0);
    }
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
});

test("stub-only and stub-with-text notifications are safely rejected", async () => {
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    for (const stub of [
      { ...message("243812345678@s.whatsapp.net"), messageStubType: 0, message: undefined },
      { ...message("243812345678@s.whatsapp.net"), messageStubType: 1 },
      {
        ...message("243812345678@s.whatsapp.net"),
        messageStubType: 2,
        message: { extendedTextMessage: { text: "stub private content" } },
      },
    ]) {
      const firstLog = logs.length;
      let attempts = 0;
      await bridge.handleInboundUpsert({ type: "notify", messages: [stub] },
        async () => { attempts += 1; return { status: 200 }; });
      assert.equal(attempts, 0);
      assert.deepEqual(namedEventsSince(firstLog, "inbound_message_skipped"), [[
        "info",
        { skip_reason: "message_stub" },
        "inbound_message_skipped",
      ]]);
      assert.equal(namedEventsSince(firstLog, "fastapi_forward_attempted").length, 0);
      assert.equal(namedEventsSince(firstLog, "fastapi_forward_succeeded").length, 0);
    }
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
});

test("invalid timestamps are rejected with fixed redacted diagnostics", async () => {
  const invalidTimestamps = [
    0,
    -1,
    NaN,
    Infinity,
    -Infinity,
    { low: 1, high: 0, unsigned: false },
    Number.MAX_VALUE,
  ];
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    for (const messageTimestamp of invalidTimestamps) {
      const firstLog = logs.length;
      let attempts = 0;
      await bridge.handleInboundUpsert({
        type: "notify",
        messages: [{ ...message("243812345678@s.whatsapp.net"), messageTimestamp }],
      }, async () => { attempts += 1; return { status: 200 }; });
      assert.equal(attempts, 0);
      assert.deepEqual(namedEventsSince(firstLog, "inbound_message_skipped"), [[
        "info",
        { skip_reason: "invalid_message_timestamp" },
        "inbound_message_skipped",
      ]]);
    }
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
});

test("positive numeric and long-like timestamps remain eligible", async () => {
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    for (const messageTimestamp of [1710000000, { toNumber: () => 1710000000 }]) {
      let attempts = 0;
      await bridge.handleInboundUpsert({
        type: "notify",
        messages: [{ ...message("243812345678@s.whatsapp.net"), messageTimestamp }],
      }, async () => { attempts += 1; return { status: 204 }; });
      assert.equal(attempts, 1);
    }
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
});

test("protocol, reaction, and unsupported notify content never reaches forwarding", async () => {
  const base = message("243812345678@s.whatsapp.net");
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    let attempts = 0;
    await bridge.handleInboundUpsert({
      type: "notify",
      messages: [
        { ...base, message: { protocolMessage: { type: 0 } } },
        { ...base, message: { reactionMessage: { text: "private reaction" } } },
        { ...base, message: { imageMessage: { caption: "private caption" } } },
      ],
    }, async () => { attempts += 1; return { status: 200 }; });
    assert.equal(attempts, 0);
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
});

test("history followed by live notify forwards only the live candidate", async () => {
  const firstLog = logs.length;
  let attempts = 0;
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    await bridge.handleInboundUpsert({
      type: "append",
      messages: [message("243812345678@s.whatsapp.net")],
    }, async () => { attempts += 1; return { status: 200 }; });
    await bridge.handleInboundUpsert({
      type: "notify",
      messages: [message("243812345678@s.whatsapp.net")],
    }, async () => { attempts += 1; return { status: 202 }; });
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
  assert.equal(attempts, 1);
  assert.equal(namedEventsSince(firstLog, "inbound_message").length, 1);
  assert.equal(namedEventsSince(firstLog, "fastapi_forward_attempted").length, 1);
  assert.equal(namedEventsSince(firstLog, "fastapi_forward_succeeded").length, 1);
});

test("successful first attempt has explicit candidate, attempt, and success semantics", async () => {
  const firstLog = logs.length;
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    await bridge.handleInboundUpsert({
      type: "notify",
      messages: [message("243812345678@s.whatsapp.net")],
    }, async () => ({ status: 201 }), { socketGeneration: 7 });
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
  const candidates = namedEventsSince(firstLog, "inbound_message");
  const attempted = namedEventsSince(firstLog, "fastapi_forward_attempted");
  const succeeded = namedEventsSince(firstLog, "fastapi_forward_succeeded");
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0][1].candidate_eligible, true);
  assert.equal(candidates[0][1].socket_generation, 7);
  assert.deepEqual(attempted.map((entry) => entry[1].attempt_number), [1]);
  assert.deepEqual(succeeded.map((entry) => entry[1]), [{
    successful_attempt_number: 1,
    socket_generation: 7,
    http_status: 201,
  }]);
  assert.equal(namedEventsSince(firstLog, "fastapi_forward_failed").length, 0);
  assert.equal(namedEventsSince(firstLog, "fastapi_forward_exhausted").length, 0);
});

test("three failed attempts log retry decisions and one exhaustion", async () => {
  const firstLog = logs.length;
  const rawErrorBody = "synthetic-raw-error-body";
  let attempts = 0;
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    await withImmediateTimeout(() => bridge.handleInboundUpsert({
      type: "notify",
      messages: [message("243812345678@s.whatsapp.net")],
    }, async () => {
      attempts += 1;
      const error = new Error("synthetic-private-error");
      error.code = "ECONNREFUSED";
      error.responseBody = rawErrorBody;
      throw error;
    }, { socketGeneration: 8 }));
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
  const attempted = namedEventsSince(firstLog, "fastapi_forward_attempted");
  const failed = namedEventsSince(firstLog, "fastapi_forward_failed");
  const exhausted = namedEventsSince(firstLog, "fastapi_forward_exhausted");
  assert.equal(attempts, 3);
  assert.deepEqual(attempted.map((entry) => entry[1].attempt_number), [1, 2, 3]);
  assert.deepEqual(failed.map((entry) => entry[1].will_retry), [true, true, false]);
  assert.ok(failed.every((entry) => entry[1].failure_category === "connection"));
  assert.deepEqual(exhausted.map((entry) => entry[1]), [{
    attempts_made: 3,
    socket_generation: 8,
    failure_category: "connection",
  }]);
  assert.equal(namedEventsSince(firstLog, "fastapi_forward_succeeded").length, 0);
  assert.equal(JSON.stringify(logs.slice(firstLog)).includes(rawErrorBody), false);
});

test("success after one retry logs one retry and succeeds on attempt two", async () => {
  const firstLog = logs.length;
  let attempts = 0;
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    await withImmediateTimeout(() => bridge.handleInboundUpsert({
      type: "notify",
      messages: [message("243812345678@s.whatsapp.net")],
    }, async () => {
      attempts += 1;
      if (attempts === 1) throw Object.assign(new Error("private timeout"), { code: "ETIMEDOUT" });
      return { status: 200, data: "private response body" };
    }, { socketGeneration: 9 }));
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
  assert.deepEqual(
    namedEventsSince(firstLog, "fastapi_forward_attempted").map((entry) => entry[1].attempt_number),
    [1, 2]
  );
  const failed = namedEventsSince(firstLog, "fastapi_forward_failed");
  assert.equal(failed.length, 1);
  assert.equal(failed[0][1].will_retry, true);
  assert.equal(failed[0][1].failure_category, "timeout");
  assert.equal(namedEventsSince(firstLog, "fastapi_forward_succeeded")[0][1].successful_attempt_number, 2);
  assert.equal(namedEventsSince(firstLog, "fastapi_forward_exhausted").length, 0);
});

test("webhook observability logs exclude identity, payload, credentials, and raw responses", async () => {
  const firstLog = logs.length;
  const sensitiveMessage = {
    key: { remoteJid: "243899999999@s.whatsapp.net", id: "full-sensitive-whatsapp-id" },
    message: { conversation: "synthetic sensitive message content" },
    messageTimestamp: 1710000000,
  };
  process.env.BAILEYS_CONNECT_ENABLED = "true";
  try {
    await bridge.handleInboundUpsert({ type: "notify", messages: [sensitiveMessage] },
      async () => ({ status: 200, data: "synthetic private response body" }));
  } finally {
    process.env.BAILEYS_CONNECT_ENABLED = "false";
  }
  const serialized = JSON.stringify(logs.slice(firstLog));
  for (const sensitive of [
    "243899999999",
    "@s.whatsapp.net",
    "synthetic sensitive message content",
    "full-sensitive-whatsapp-id",
    "Authorization",
    "test-webhook-secret",
    "customer_phone",
    "synthetic private response body",
    "/app/sessions",
    "qrDataUrl",
  ]) assert.equal(serialized.includes(sensitive), false);
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

class FakeEmitter {
  constructor() { this.listeners = new Map(); }
  on(event, handler) {
    const handlers = this.listeners.get(event) || [];
    handlers.push(handler);
    this.listeners.set(event, handlers);
  }
  emit(event, value) {
    for (const handler of [...(this.listeners.get(event) || [])]) handler(value);
  }
  removeAllListeners(event) { this.listeners.delete(event); }
}

function fakeSocket(overrides = {}) {
  return {
    ev: new FakeEmitter(),
    user: null,
    endCalls: 0,
    logoutCalls: 0,
    end() { this.endCalls += 1; },
    async logout() { this.logoutCalls += 1; },
    ...overrides,
  };
}

function fakeTimers() {
  let nextId = 1;
  const pending = new Map();
  const delays = [];
  return {
    setTimer(fn, delay) {
      const id = nextId++;
      pending.set(id, fn);
      delays.push(delay);
      return id;
    },
    clearTimer(id) { pending.delete(id); },
    fireNext() {
      const entry = pending.entries().next().value;
      if (!entry) return false;
      pending.delete(entry[0]);
      entry[1]();
      return true;
    },
    get size() { return pending.size; },
    delays,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

function enableLifecycle() {
  bridge.resetLifecycleForTests();
  process.env.BAILEYS_CONNECT_ENABLED = "true";
}

function disableLifecycle() {
  bridge.resetLifecycleForTests();
  process.env.BAILEYS_CONNECT_ENABLED = "false";
}

test("single-flight concurrent connects reuse one import, auth state, version lookup, and socket", async () => {
  enableLifecycle();
  const moduleImport = deferred();
  const calls = { import: 0, auth: 0, version: 0, socket: 0 };
  const socket = fakeSocket();
  const v7LikeAuthState = {
    creds: { registered: true },
    keys: {
      get: async () => ({}),
      set: async () => {},
    },
  };
  let socketAuth;
  bridge.setBaileysImporterForTests(() => {
    calls.import += 1;
    return moduleImport.promise;
  });
  const first = bridge.connectToWhatsApp();
  const second = bridge.connectToWhatsApp();
  assert.equal(first, second);
  await settle();
  assert.deepEqual(calls, { import: 1, auth: 0, version: 0, socket: 0 });
  moduleImport.resolve({
    ...fakeBaileysModule,
    useMultiFileAuthState: async () => {
      calls.auth += 1;
      return { state: v7LikeAuthState, saveCreds() {} };
    },
    fetchLatestWaWebVersion: async () => {
      calls.version += 1;
      return { version: [1, 2, 3] };
    },
    makeWASocket: (options) => {
      calls.socket += 1;
      socketAuth = options.auth;
      return socket;
    },
  });
  assert.equal(await first, socket);
  assert.deepEqual(calls, { import: 1, auth: 1, version: 1, socket: 1 });
  assert.equal(socketAuth, v7LikeAuthState);
  assert.equal(bridge.getLifecycleStateForTests().connectPromise, null);
  bridge.setBaileysModuleForTests(fakeBaileysModule);
  disableLifecycle();
});

test("async ESM socket factory is awaited before lifecycle registration", async () => {
  enableLifecycle();
  const socket = fakeSocket();
  let factoryResolved = false;
  bridge.setLifecycleDependenciesForTests({
    loadAuth: async () => ({ state: {}, saveCreds() {} }),
    fetchVersion: async () => [1, 2, 3],
    makeSocket: async () => {
      await new Promise((resolve) => setImmediate(resolve));
      factoryResolved = true;
      return socket;
    },
  });
  const connected = await bridge.connectToWhatsApp();
  assert.equal(factoryResolved, true);
  assert.equal(connected, socket);
  assert.equal(bridge.getLifecycleStateForTests().currentSocket, socket);
  disableLifecycle();
});

test("failed dynamic import clears connection state and permits a controlled retry", async () => {
  enableLifecycle();
  let importCalls = 0;
  const socket = fakeSocket();
  bridge.setBaileysImporterForTests(async () => {
    importCalls += 1;
    if (importCalls === 1) throw new Error("mock import failure");
    return {
      ...fakeBaileysModule,
      useMultiFileAuthState: async () => ({ state: {}, saveCreds() {} }),
      fetchLatestWaWebVersion: async () => ({ version: [1, 2, 3] }),
      makeWASocket: () => socket,
    };
  });
  await assert.rejects(bridge.connectToWhatsApp(), /mock import failure/);
  assert.equal(bridge.getLifecycleStateForTests().connectPromise, null);
  await assert.doesNotReject(() => bridge.connectToWhatsApp());
  assert.equal(importCalls, 2);
  bridge.setBaileysModuleForTests(fakeBaileysModule);
  disableLifecycle();
});

test("repeated recoverable close events create exactly one reconnect timer", async () => {
  enableLifecycle();
  const timers = fakeTimers();
  const socket = fakeSocket();
  bridge.setLifecycleDependenciesForTests({
    loadAuth: async () => ({ state: {}, saveCreds() {} }),
    fetchVersion: async () => undefined,
    makeSocket: () => socket,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    random: () => 0,
  });
  await bridge.connectToWhatsApp();
  socket.ev.emit("connection.update", { connection: "close" });
  socket.ev.emit("connection.update", { connection: "close" });
  assert.equal(timers.size, 1);
  assert.equal(bridge.getLifecycleStateForTests().reconnectAttempt, 1);
  disableLifecycle();
});

test("close while a connect is pending never overlaps socket creation", async () => {
  enableLifecycle();
  const timers = fakeTimers();
  const oldSocket = fakeSocket();
  bridge.setSocketForTests(oldSocket);
  const oldGeneration = bridge.getLifecycleStateForTests().socketGeneration;
  const auth = deferred();
  let socketCalls = 0;
  bridge.setLifecycleDependenciesForTests({
    loadAuth: () => auth.promise,
    fetchVersion: async () => undefined,
    makeSocket: () => { socketCalls += 1; return fakeSocket(); },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  const pending = bridge.connectToWhatsApp();
  bridge.handleConnectionUpdate(oldSocket, oldGeneration, { connection: "close" });
  assert.equal(timers.size, 0);
  assert.equal(socketCalls, 0);
  auth.resolve({ state: {}, saveCreds() {} });
  await pending;
  assert.equal(socketCalls, 1);
  disableLifecycle();
});

test("stale socket events cannot mutate, reconnect, or persist credentials for a newer generation", async () => {
  enableLifecycle();
  const timers = fakeTimers();
  const sockets = [fakeSocket(), fakeSocket()];
  const credentialSaves = [0, 0];
  let authLoads = 0;
  bridge.setLifecycleDependenciesForTests({
    loadAuth: async () => {
      const index = authLoads;
      authLoads += 1;
      return {
        state: {},
        saveCreds() { credentialSaves[index] += 1; },
      };
    },
    fetchVersion: async () => undefined,
    makeSocket: () => sockets.shift(),
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  const socketA = await bridge.connectToWhatsApp();
  const generationA = bridge.getLifecycleStateForTests().socketGeneration;
  socketA.ev.emit("creds.update", {});
  assert.deepEqual(credentialSaves, [1, 0]);
  const socketB = await bridge.connectToWhatsApp();
  const stateB = bridge.getLifecycleStateForTests();
  socketA.ev.emit("creds.update", {});
  socketB.ev.emit("creds.update", {});
  bridge.handleConnectionUpdate(socketA, generationA, { connection: "close", qr: "stale-secret-qr" });
  const after = bridge.getLifecycleStateForTests();
  assert.equal(after.currentSocket, socketB);
  assert.equal(after.socketGeneration, stateB.socketGeneration);
  assert.equal(after.currentQR, null);
  assert.equal(timers.size, 0);
  assert.equal(socketA.endCalls, 1);
  assert.deepEqual(credentialSaves, [1, 1]);
  disableLifecycle();
});

test("authoritative open clears QR and timer and resets backoff", async () => {
  enableLifecycle();
  const timers = fakeTimers();
  const socket = fakeSocket();
  bridge.setLifecycleDependenciesForTests({
    loadAuth: async () => ({ state: {}, saveCreds() {} }),
    fetchVersion: async () => undefined,
    makeSocket: () => socket,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    random: () => 0,
  });
  await bridge.connectToWhatsApp();
  socket.ev.emit("connection.update", { qr: "secret-qr" });
  assert.equal(bridge.scheduleReconnect(), true);
  socket.ev.emit("connection.update", { connection: "open" });
  const state = bridge.getLifecycleStateForTests();
  assert.equal(state.currentSocket, socket);
  assert.equal(state.currentQR, null);
  assert.equal(state.reconnectAttempt, 0);
  assert.equal(timers.size, 0);
  disableLifecycle();
});

test("logged-out close records suppression and schedules nothing", async () => {
  enableLifecycle();
  const timers = fakeTimers();
  const socket = fakeSocket();
  bridge.setLifecycleDependenciesForTests({
    loadAuth: async () => ({ state: {}, saveCreds() {} }),
    fetchVersion: async () => undefined,
    makeSocket: () => socket,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  await bridge.connectToWhatsApp();
  socket.ev.emit("connection.update", {
    connection: "close",
    lastDisconnect: { error: { output: { statusCode: 401 } } },
  });
  assert.equal(bridge.getLifecycleStateForTests().loggedOut, true);
  assert.equal(bridge.getLifecycleStateForTests().terminalDisconnectReason, "logged_out");
  assert.equal(timers.size, 0);
  disableLifecycle();
});

test("recoverable v7 disconnects use truthful categories and one bounded timer", async () => {
  assert.equal(
    fakeBaileysModule.DisconnectReason.connectionLost,
    fakeBaileysModule.DisconnectReason.timedOut
  );
  const cases = [
    [{ lastDisconnect: { error: { output: { statusCode: 515 }, private: "restart-private" } } },
      "restart_required", "baileys.reconnect_scheduled", 1],
    [{ lastDisconnect: { error: { output: { statusCode: 408 }, private: "408-private" } } },
      "connection_lost_or_timed_out", "baileys.reconnect_scheduled", 1],
    [{ lastDisconnect: { error: { output: { statusCode: 428 }, private: "closed-private" } } },
      "connection_closed", "baileys.reconnect_scheduled", 1],
    [{ lastDisconnect: { error: { output: { statusCode: 503 }, private: "unavailable-private" } } },
      "unavailable_service", "baileys.reconnect_scheduled", 1],
    [{ lastDisconnect: { error: { output: { statusCode: 499 }, private: "generic-private" } } },
      "recoverable_close", "baileys.reconnect_scheduled", 1],
    [{ lastDisconnect: { error: { private: "unknown-private" } } },
      "unknown", "baileys.reconnect_scheduled", 1],
  ];

  for (const [closeFields, category, eventName, expectedTimers] of cases) {
    enableLifecycle();
    const firstLog = logs.length;
    const timers = fakeTimers();
    const socket = fakeSocket();
    bridge.setLifecycleDependenciesForTests({
      loadAuth: async () => ({ state: {}, saveCreds() {} }),
      fetchVersion: async () => undefined,
      makeSocket: () => socket,
      setTimer: timers.setTimer,
      clearTimer: timers.clearTimer,
      random: () => 0,
    });
    await bridge.connectToWhatsApp();
    const connectStarted = namedEventsSince(firstLog, "baileys.connect_started");
    assert.equal(connectStarted.length, 1);
    assert.deepEqual(connectStarted[0][1], {
      next_socket_generation: 1,
      reconnect_attempt: 0,
      initiating_category: "manual_or_startup",
    });
    socket.ev.emit("connection.update", { connection: "close", ...closeFields });
    const lifecycleEvent = namedEventsSince(firstLog, eventName).at(-1);
    assert.equal(lifecycleEvent[1].socket_generation, 1);
    assert.equal(lifecycleEvent[1].disconnect_category, category);
    assert.ok(lifecycleEvent[1].delay_ms >= 1000 && lifecycleEvent[1].delay_ms <= 30000);
    assert.equal(timers.size, expectedTimers);
    const serialized = JSON.stringify(logs.slice(firstLog));
    for (const sensitive of [
      "restart-private",
      "408-private",
      "closed-private",
      "unavailable-private",
      "generic-private",
      "unknown-private",
    ]) {
      assert.equal(serialized.includes(sensitive), false);
    }
    disableLifecycle();
  }
});

test("restart-required timer creates exactly one next connection without terminal suppression", async () => {
  enableLifecycle();
  const timers = fakeTimers();
  const sockets = [fakeSocket(), fakeSocket()];
  let authLoads = 0;
  let socketCreations = 0;
  bridge.setLifecycleDependenciesForTests({
    loadAuth: async () => { authLoads += 1; return { state: {}, saveCreds() {} }; },
    fetchVersion: async () => undefined,
    makeSocket: () => { socketCreations += 1; return sockets.shift(); },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    random: () => 0,
  });
  const first = await bridge.connectToWhatsApp();
  first.ev.emit("connection.update", {
    connection: "close",
    lastDisconnect: { error: { output: { statusCode: 515 } } },
  });
  assert.equal(timers.size, 1);
  assert.equal(timers.fireNext(), true);
  await settle();
  const state = bridge.getLifecycleStateForTests();
  assert.equal(socketCreations, 2);
  assert.equal(authLoads, 2);
  assert.equal(state.socketGeneration, 2);
  assert.equal(state.terminalDisconnectReason, null);
  disableLifecycle();
});

test("terminal v7 disconnects suppress reconnect and preserve session state", async () => {
  const fs = require("fs");
  const terminalCases = [
    [401, "logged_out", "manual_reauthentication_required"],
    [440, "connection_replaced", "another_connection_replaced_this_session"],
    [500, "bad_session", "manual_session_review_required"],
    [411, "multidevice_mismatch", "manual_compatibility_review_required"],
  ];

  for (const [statusCode, category, action] of terminalCases) {
    enableLifecycle();
    const firstLog = logs.length;
    const timers = fakeTimers();
    const socket = fakeSocket();
    let authLoads = 0;
    let credentialSaves = 0;
    let sessionDeletes = 0;
    let sessionCreates = 0;
    const originals = { rmSync: fs.rmSync, mkdirSync: fs.mkdirSync };
    fs.rmSync = () => { sessionDeletes += 1; };
    fs.mkdirSync = () => { sessionCreates += 1; };
    bridge.setLifecycleDependenciesForTests({
      loadAuth: async () => {
        authLoads += 1;
        return { state: {}, saveCreds() { credentialSaves += 1; } };
      },
      fetchVersion: async () => undefined,
      makeSocket: () => socket,
      setTimer: timers.setTimer,
      clearTimer: timers.clearTimer,
      random: () => 0,
    });
    try {
      await bridge.connectToWhatsApp();
      socket.ev.emit("connection.update", {
        connection: "close",
        lastDisconnect: { error: { output: { statusCode }, raw: "terminal-private" } },
      });
      assert.equal(timers.size, 0);
      assert.equal(bridge.getLifecycleStateForTests().terminalDisconnectReason, category);
      await bridge.connectToWhatsApp();
      assert.equal(authLoads, 1);
      assert.equal(credentialSaves, 0);
      assert.equal(sessionDeletes, 0);
      assert.equal(sessionCreates, 0);
      assert.equal(socket.logoutCalls, 0);
      const terminalEvent = namedEventsSince(firstLog, "baileys.disconnect_terminal").at(-1);
      assert.deepEqual(terminalEvent[1], {
        socket_generation: 1,
        disconnect_category: category,
        action,
      });
      assert.equal(JSON.stringify(logs.slice(firstLog)).includes("terminal-private"), false);
    } finally {
      fs.rmSync = originals.rmSync;
      fs.mkdirSync = originals.mkdirSync;
      disableLifecycle();
    }
  }
});

test("stale terminal closes cannot suppress a newer socket", async () => {
  for (const statusCode of [440, 500, 411]) {
    enableLifecycle();
    const timers = fakeTimers();
    const staleSocket = fakeSocket();
    const currentSocket = fakeSocket();
    bridge.setLifecycleDependenciesForTests({
      setTimer: timers.setTimer,
      clearTimer: timers.clearTimer,
    });
    bridge.setSocketForTests(staleSocket);
    const staleGeneration = bridge.getLifecycleStateForTests().socketGeneration;
    bridge.setSocketForTests(currentSocket);
    bridge.handleConnectionUpdate(staleSocket, staleGeneration, {
      connection: "close",
      lastDisconnect: { error: { output: { statusCode } } },
    });
    const state = bridge.getLifecycleStateForTests();
    assert.equal(state.currentSocket, currentSocket);
    assert.equal(state.terminalDisconnectReason, null);
    assert.equal(timers.size, 0);
    disableLifecycle();
  }
});

test("terminal close clears an existing reconnect timer before it can connect", async () => {
  enableLifecycle();
  const timers = fakeTimers();
  const socket = fakeSocket();
  let authLoads = 0;
  bridge.setLifecycleDependenciesForTests({
    loadAuth: async () => { authLoads += 1; return { state: {}, saveCreds() {} }; },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    random: () => 0,
  });
  bridge.setSocketForTests(socket);
  const generation = bridge.getLifecycleStateForTests().socketGeneration;
  assert.equal(bridge.scheduleReconnect("connection_lost_or_timed_out", generation), true);
  assert.equal(timers.size, 1);
  bridge.handleConnectionUpdate(socket, generation, {
    connection: "close",
    lastDisconnect: { error: { output: { statusCode: 500 } } },
  });
  assert.equal(timers.size, 0);
  assert.equal(timers.fireNext(), false);
  await settle();
  assert.equal(authLoads, 0);
  assert.equal(bridge.getLifecycleStateForTests().terminalDisconnectReason, "bad_session");
  disableLifecycle();
});

test("duplicate closes retain one timer and report stale generation safely", async () => {
  enableLifecycle();
  const firstLog = logs.length;
  const timers = fakeTimers();
  const socket = fakeSocket();
  bridge.setLifecycleDependenciesForTests({
    loadAuth: async () => ({ state: {}, saveCreds() {} }),
    fetchVersion: async () => undefined,
    makeSocket: () => socket,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    random: () => 0,
  });
  await bridge.connectToWhatsApp();
  socket.ev.emit("connection.update", { connection: "close" });
  socket.ev.emit("connection.update", { connection: "close" });
  assert.equal(timers.size, 1);
  const skipped = namedEventsSince(firstLog, "baileys.reconnect_skipped").at(-1);
  assert.equal(skipped[1].skip_reason, "stale_generation");
  assert.equal(skipped[1].socket_generation, 1);
  assert.equal(skipped[1].authoritative_socket_generation, 1);
  disableLifecycle();
});

test("logout clears reconnect and close caused by logout cannot reconnect", async () => {
  enableLifecycle();
  process.env.BAILEYS_LOGOUT_ENABLED = "true";
  process.env.BAILEYS_RECOVERY_TOKEN = "test-recovery-token";
  const fs = require("fs");
  const timers = fakeTimers();
  const socket = fakeSocket({ user: { id: "redacted@s.whatsapp.net" } });
  bridge.setLifecycleDependenciesForTests({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    random: () => 0,
  });
  bridge.setSocketForTests(socket);
  bridge.scheduleReconnect();
  const originals = { rmSync: fs.rmSync, mkdirSync: fs.mkdirSync };
  fs.rmSync = () => {};
  fs.mkdirSync = () => {};
  const res = response();
  try { await routes.post["/logout"](recoveryRequest(), res); } finally {
    fs.rmSync = originals.rmSync;
    fs.mkdirSync = originals.mkdirSync;
  }
  socket.ev.emit("connection.update", { connection: "close" });
  assert.equal(res.body.success, true);
  assert.equal(socket.logoutCalls, 1);
  assert.equal(timers.size, 0);
  assert.equal(bridge.getLifecycleStateForTests().loggedOut, true);
  assert.equal(bridge.getLifecycleStateForTests().terminalDisconnectReason, "logged_out");
  delete process.env.BAILEYS_LOGOUT_ENABLED;
  delete process.env.BAILEYS_RECOVERY_TOKEN;
  disableLifecycle();
});

test("logout suppresses an in-flight connection before socket creation", async () => {
  enableLifecycle();
  process.env.BAILEYS_LOGOUT_ENABLED = "true";
  process.env.BAILEYS_RECOVERY_TOKEN = "test-recovery-token";
  const fs = require("fs");
  const auth = deferred();
  let socketCalls = 0;
  bridge.setLifecycleDependenciesForTests({
    loadAuth: () => auth.promise,
    fetchVersion: async () => undefined,
    makeSocket: () => { socketCalls += 1; return fakeSocket(); },
  });
  const pending = bridge.connectToWhatsApp();
  const originals = { rmSync: fs.rmSync, mkdirSync: fs.mkdirSync };
  fs.rmSync = () => {};
  fs.mkdirSync = () => {};
  const res = response();
  try { await routes.post["/logout"](recoveryRequest(), res); } finally {
    fs.rmSync = originals.rmSync;
    fs.mkdirSync = originals.mkdirSync;
  }
  auth.resolve({ state: {}, saveCreds() {} });
  assert.equal(await pending, undefined);
  assert.equal(res.body.success, true);
  assert.equal(socketCalls, 0);
  assert.equal(bridge.getLifecycleStateForTests().loggedOut, true);
  assert.equal(bridge.getLifecycleStateForTests().terminalDisconnectReason, "logged_out");
  delete process.env.BAILEYS_LOGOUT_ENABLED;
  delete process.env.BAILEYS_RECOVERY_TOKEN;
  disableLifecycle();
});

test("shutdown clears timers, ends socket and server, and never logs out", async () => {
  enableLifecycle();
  const timers = fakeTimers();
  const socket = fakeSocket({ user: { id: "redacted@s.whatsapp.net" } });
  let serverCloseCalls = 0;
  const server = { close(callback) { serverCloseCalls += 1; callback(); } };
  bridge.setLifecycleDependenciesForTests({
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    random: () => 0,
  });
  bridge.setSocketForTests(socket);
  const generation = bridge.getLifecycleStateForTests().socketGeneration;
  bridge.scheduleReconnect();
  await bridge.shutdownBridge(server);
  bridge.handleConnectionUpdate(socket, generation, {
    connection: "close",
    lastDisconnect: { error: { output: { statusCode: 500 } } },
  });
  assert.equal(timers.size, 0);
  assert.equal(timers.fireNext(), false);
  assert.equal(socket.endCalls, 1);
  assert.equal(socket.logoutCalls, 0);
  assert.equal(serverCloseCalls, 1);
  assert.equal(bridge.getLifecycleStateForTests().stopping, true);
  assert.equal(bridge.getLifecycleStateForTests().terminalDisconnectReason, null);
  disableLifecycle();
});

test("reconnect callback catches rejection and schedules bounded retry", async () => {
  enableLifecycle();
  const firstLog = logs.length;
  const timers = fakeTimers();
  bridge.setLifecycleDependenciesForTests({
    loadAuth: async () => { throw new Error("mock reconnect rejection"); },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    random: () => 0,
  });
  assert.equal(bridge.scheduleReconnect(), true);
  assert.equal(timers.fireNext(), true);
  await settle();
  assert.equal(timers.size, 1);
  assert.match(JSON.stringify(logs.slice(firstLog)), /baileys\.reconnect_failed/);
  disableLifecycle();
});

test("backoff is bounded and successful open resets attempt count", async () => {
  enableLifecycle();
  const timers = fakeTimers();
  bridge.setLifecycleDependenciesForTests({
    loadAuth: async () => { throw new Error("retry"); },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    random: () => 0.999,
  });
  for (let i = 0; i < 8; i += 1) {
    if (timers.size === 0) bridge.scheduleReconnect();
    timers.fireNext();
    await settle();
  }
  assert.ok(timers.delays.every((delay) => delay >= 1000 && delay <= 30000));
  const socket = fakeSocket();
  bridge.setSocketForTests(socket);
  const generation = bridge.getLifecycleStateForTests().socketGeneration;
  bridge.handleConnectionUpdate(socket, generation, { connection: "open" });
  assert.equal(bridge.getLifecycleStateForTests().reconnectAttempt, 0);
  disableLifecycle();
});

test("connection-disabled lifecycle touches no auth, version, socket or timer", async () => {
  disableLifecycle();
  const timers = fakeTimers();
  const calls = { auth: 0, version: 0, socket: 0 };
  bridge.setLifecycleDependenciesForTests({
    loadAuth: async () => { calls.auth += 1; },
    fetchVersion: async () => { calls.version += 1; },
    makeSocket: () => { calls.socket += 1; },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  await bridge.connectToWhatsApp();
  assert.equal(bridge.scheduleReconnect(), false);
  assert.deepEqual(calls, { auth: 0, version: 0, socket: 0 });
  assert.equal(timers.size, 0);
});

test("enabled QR endpoints fail closed without configured recovery token", async () => {
  bridge.resetLifecycleForTests();
  process.env.BAILEYS_QR_ENDPOINTS_ENABLED = "true";
  delete process.env.BAILEYS_RECOVERY_TOKEN;
  const res = response();
  await routes.get["/qr.json"]({}, res);
  assert.equal(res.statusCode, 503);
  assert.deepEqual(res.body, { error: "recovery_auth_unconfigured" });
  delete process.env.BAILEYS_QR_ENDPOINTS_ENABLED;
});

test("QR endpoints reject missing and incorrect Basic authentication", async () => {
  bridge.resetLifecycleForTests();
  process.env.BAILEYS_QR_ENDPOINTS_ENABLED = "true";
  process.env.BAILEYS_RECOVERY_TOKEN = "test-recovery-token";
  const firstLog = logs.length;
  for (const route of [routes.get["/qr"], routes.get["/qr.json"]]) {
    for (const req of [{}, recoveryRequest("incorrect-token")]) {
      const denied = response();
      await route(req, denied);
      assert.equal(denied.statusCode, 401);
      assert.deepEqual(denied.body, { error: "recovery_auth_required" });
      assert.match(denied.headers["WWW-Authenticate"], /^Basic /);
    }
  }
  const events = auditEventsSince(firstLog, "baileys.protected_route_rejected");
  assert.equal(events.length, 4);
  for (const event of events) {
    assert.deepEqual(event, [
      "warn",
      { route_category: "qr", reason: "missing_or_invalid_auth" },
      "baileys.protected_route_rejected",
    ]);
  }
  const serialized = JSON.stringify(events);
  for (const sensitive of [
    "incorrect-token", "Authorization", "test-recovery-token", "qrDataUrl",
    "@s.whatsapp.net", "phone", "/app/sessions", "header",
  ]) assert.equal(serialized.includes(sensitive), false);
  delete process.env.BAILEYS_QR_ENDPOINTS_ENABLED;
  delete process.env.BAILEYS_RECOVERY_TOKEN;
});

test("authenticated QR routes expose mocked QR state without identity", async () => {
  const firstLog = logs.length;
  enableLifecycle();
  process.env.BAILEYS_QR_ENDPOINTS_ENABLED = "true";
  process.env.BAILEYS_RECOVERY_TOKEN = "test-recovery-token";
  const socket = fakeSocket({ user: { id: "243899999999@s.whatsapp.net" } });
  bridge.setSocketForTests(socket);
  const generation = bridge.getLifecycleStateForTests().socketGeneration;
  bridge.handleConnectionUpdate(socket, generation, { qr: "mock-current-qr" });

  const jsonRes = response();
  await routes.get["/qr.json"](recoveryRequest(), jsonRes);
  assert.equal(jsonRes.statusCode, 200);
  assert.equal(jsonRes.body.connected, true);
  assert.equal(jsonRes.body.qrDataUrl, "data:image/png;base64,mocked");
  assert.ok(jsonRes.body.ts > 0);
  assert.equal("jid" in jsonRes.body, false);

  const pageRes = response();
  routes.get["/qr"](recoveryRequest(), pageRes);
  assert.equal(pageRes.statusCode, 200);
  assert.equal(pageRes.body.includes("/qr.json"), true);
  assert.equal(pageRes.body.includes("243899999999"), false);
  assert.equal(pageRes.body.includes("jid-label"), false);

  bridge.handleConnectionUpdate(socket, generation, { connection: "open" });
  const afterOpen = response();
  await routes.get["/qr.json"](recoveryRequest(), afterOpen);
  assert.equal(afterOpen.body.qrDataUrl, null);
  assert.equal(afterOpen.body.ts, 0);
  assert.equal(auditEventsSince(firstLog, "baileys.protected_route_rejected").length, 0);
  delete process.env.BAILEYS_QR_ENDPOINTS_ENABLED;
  delete process.env.BAILEYS_RECOVERY_TOKEN;
  disableLifecycle();
});

test("stale generation QR cannot reach authenticated pairing responses", async () => {
  enableLifecycle();
  process.env.BAILEYS_QR_ENDPOINTS_ENABLED = "true";
  process.env.BAILEYS_RECOVERY_TOKEN = "test-recovery-token";
  const socketA = fakeSocket();
  const socketB = fakeSocket();
  bridge.setSocketForTests(socketA);
  const generationA = bridge.getLifecycleStateForTests().socketGeneration;
  bridge.setSocketForTests(socketB);
  bridge.handleConnectionUpdate(socketA, generationA, { qr: "stale-route-qr" });
  const res = response();
  await routes.get["/qr.json"](recoveryRequest(), res);
  assert.equal(res.body.qrDataUrl, null);
  assert.equal(res.body.ts, 0);
  delete process.env.BAILEYS_QR_ENDPOINTS_ENABLED;
  delete process.env.BAILEYS_RECOVERY_TOKEN;
  disableLifecycle();
});

test("root and health expose operational state without account identity", () => {
  enableLifecycle();
  bridge.setSocketForTests(fakeSocket({ user: { id: "243877777777@s.whatsapp.net" } }));
  for (const path of ["/", "/health"]) {
    const res = response();
    routes.get[path]({}, res);
    const serialized = JSON.stringify(res.body);
    assert.equal(serialized.includes("243877777777"), false);
    assert.equal(serialized.toLowerCase().includes("jid"), false);
    assert.equal(serialized.toLowerCase().includes("qr"), false);
    assert.equal(serialized.includes("/app/sessions"), false);
    if (path === "/health") assert.equal(res.body.connection, "connected");
  }
  disableLifecycle();
});

test("disabled logout ignores even valid recovery credentials with zero effects", async () => {
  const firstLog = logs.length;
  enableLifecycle();
  process.env.BAILEYS_RECOVERY_TOKEN = "test-recovery-token";
  delete process.env.BAILEYS_LOGOUT_ENABLED;
  const fs = require("fs");
  const timers = fakeTimers();
  const socket = fakeSocket({ user: { id: "redacted@s.whatsapp.net" } });
  bridge.setSocketForTests(socket);
  bridge.setLifecycleDependenciesForTests({ setTimer: timers.setTimer, clearTimer: timers.clearTimer });
  let fileCalls = 0;
  const originals = { rmSync: fs.rmSync, mkdirSync: fs.mkdirSync };
  fs.rmSync = () => { fileCalls += 1; };
  fs.mkdirSync = () => { fileCalls += 1; };
  const res = response();
  try { await routes.post["/logout"](recoveryRequest(), res); } finally {
    fs.rmSync = originals.rmSync;
    fs.mkdirSync = originals.mkdirSync;
  }
  assert.equal(res.statusCode, 404);
  assert.equal(socket.logoutCalls, 0);
  assert.equal(fileCalls, 0);
  assert.equal(bridge.getLifecycleStateForTests().currentSocket, socket);
  assert.equal(timers.size, 0);
  const events = auditEventsSince(firstLog, "baileys.logout_disabled");
  assert.deepEqual(events, [[
    "warn",
    { action: "logout", reason: "disabled" },
    "baileys.logout_disabled",
  ]]);
  delete process.env.BAILEYS_RECOVERY_TOKEN;
  disableLifecycle();
});

test("enabled logout requires authentication before lifecycle or session effects", async () => {
  const firstLog = logs.length;
  enableLifecycle();
  process.env.BAILEYS_LOGOUT_ENABLED = "true";
  process.env.BAILEYS_RECOVERY_TOKEN = "test-recovery-token";
  const fs = require("fs");
  const socket = fakeSocket({ user: { id: "redacted@s.whatsapp.net" } });
  bridge.setSocketForTests(socket);
  let fileCalls = 0;
  const originals = { rmSync: fs.rmSync, mkdirSync: fs.mkdirSync };
  fs.rmSync = () => { fileCalls += 1; };
  fs.mkdirSync = () => { fileCalls += 1; };
  try {
    for (const req of [{}, recoveryRequest("wrong-token")]) {
      const denied = response();
      await routes.post["/logout"](req, denied);
      assert.equal(denied.statusCode, 401);
      assert.equal(socket.logoutCalls, 0);
      assert.equal(fileCalls, 0);
      assert.equal(bridge.getLifecycleStateForTests().currentSocket, socket);
    }
    const allowed = response();
    await routes.post["/logout"](recoveryRequest(), allowed);
    assert.equal(allowed.body.success, true);
    assert.equal(socket.logoutCalls, 1);
    assert.equal(fileCalls, 2);
    assert.equal(bridge.getLifecycleStateForTests().loggedOut, true);
    assert.equal(bridge.getLifecycleStateForTests().currentQR, null);
    assert.equal(auditEventsSince(firstLog, "baileys.logout_disabled").length, 0);
  } finally {
    fs.rmSync = originals.rmSync;
    fs.mkdirSync = originals.mkdirSync;
  }
  delete process.env.BAILEYS_LOGOUT_ENABLED;
  delete process.env.BAILEYS_RECOVERY_TOKEN;
  disableLifecycle();
});

test("pairing and logout credentials never appear in logs or denied responses", async () => {
  const firstLog = logs.length;
  const token = "sensitive-recovery-token-value";
  process.env.BAILEYS_QR_ENDPOINTS_ENABLED = "true";
  process.env.BAILEYS_LOGOUT_ENABLED = "true";
  process.env.BAILEYS_RECOVERY_TOKEN = token;
  const responses = [];
  for (const [route, req] of [
    [routes.get["/qr.json"], recoveryRequest("wrong-sensitive-token")],
    [routes.post["/logout"], recoveryRequest("wrong-sensitive-token")],
  ]) {
    const res = response();
    await route(req, res);
    responses.push(res.body);
  }
  const serialized = JSON.stringify([logs.slice(firstLog), responses]);
  assert.equal(serialized.includes(token), false);
  assert.equal(serialized.includes("wrong-sensitive-token"), false);
  delete process.env.BAILEYS_QR_ENDPOINTS_ENABLED;
  delete process.env.BAILEYS_LOGOUT_ENABLED;
  delete process.env.BAILEYS_RECOVERY_TOKEN;
});

test("lifecycle logs contain no QR, JID, session detail or raw rejection", () => {
  const serialized = JSON.stringify(logs);
  for (const sensitive of [
    "secret-qr",
    "stale-secret-qr",
    "redacted@s.whatsapp.net",
    "mock reconnect rejection",
    "/app/sessions",
  ]) {
    assert.equal(serialized.includes(sensitive), false);
  }
});
