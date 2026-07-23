"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const SCHEMA_VERSION = 1;
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const HASH_RE = /^[0-9a-f]{64}$/;
const RECORD_KEYS = new Set([
  "schema_version",
  "key_hash",
  "payload_fingerprint",
  "state",
  "provider_message_id",
  "created_at",
  "updated_at",
]);

class OutboundLedgerError extends Error {
  constructor(code) {
    super(code);
    this.name = "OutboundLedgerError";
    this.code = code;
  }
}

function hashValue(value) {
  return crypto.createHash("sha256").update(value, "utf8").digest("hex");
}

function validateIdempotencyKey(value) {
  return typeof value === "string" && UUID_RE.test(value);
}

function payloadFingerprint(normalizedDestination, message) {
  if (
    typeof normalizedDestination !== "string"
    || !normalizedDestination
    || typeof message !== "string"
    || !message
  ) {
    throw new OutboundLedgerError("invalid_payload");
  }
  return hashValue(`${normalizedDestination}\u0000${message}`);
}

function isIsoTimestamp(value) {
  return (
    typeof value === "string"
    && value.length <= 40
    && Number.isFinite(Date.parse(value))
  );
}

function validateRecord(record, expectedKeyHash) {
  if (!record || typeof record !== "object" || Array.isArray(record)) {
    throw new OutboundLedgerError("ledger_record_invalid");
  }
  if (Object.keys(record).some((key) => !RECORD_KEYS.has(key))) {
    throw new OutboundLedgerError("ledger_record_invalid");
  }
  if (
    record.schema_version !== SCHEMA_VERSION
    || record.key_hash !== expectedKeyHash
    || !HASH_RE.test(record.key_hash)
    || !HASH_RE.test(record.payload_fingerprint)
    || !["in_progress", "sent", "unknown"].includes(record.state)
    || !isIsoTimestamp(record.created_at)
  ) {
    throw new OutboundLedgerError("ledger_record_invalid");
  }
  if (record.updated_at !== undefined && !isIsoTimestamp(record.updated_at)) {
    throw new OutboundLedgerError("ledger_record_invalid");
  }
  if (
    record.state === "sent"
    && (
      typeof record.provider_message_id !== "string"
      || !record.provider_message_id.trim()
      || record.provider_message_id.length > 512
    )
  ) {
    throw new OutboundLedgerError("ledger_record_invalid");
  }
  if (record.state !== "sent" && record.provider_message_id !== undefined) {
    throw new OutboundLedgerError("ledger_record_invalid");
  }
  return record;
}

class OutboundLedger {
  constructor(ledgerDirectory, options = {}) {
    this.fs = options.fs || fs;
    this.now = options.now || (() => new Date().toISOString());
    this.ledgerDirectory =
      typeof ledgerDirectory === "string" && ledgerDirectory
        ? path.resolve(ledgerDirectory)
        : "";
    this.forbiddenDirectories = (options.forbiddenDirectories || [])
      .filter((entry) => typeof entry === "string" && entry)
      .map((entry) => path.resolve(entry));
  }

  ensureAvailable() {
    if (!this.ledgerDirectory) {
      throw new OutboundLedgerError("ledger_unconfigured");
    }
    let stat;
    try {
      stat = this.fs.statSync(this.ledgerDirectory);
      if (!stat.isDirectory()) throw new Error("not_directory");
      this.fs.accessSync(
        this.ledgerDirectory,
        this.fs.constants.R_OK | this.fs.constants.W_OK,
      );
      const ledgerRealPath = this.fs.realpathSync(this.ledgerDirectory);
      for (const forbidden of this.forbiddenDirectories) {
        let forbiddenRealPath = forbidden;
        try {
          forbiddenRealPath = this.fs.realpathSync(forbidden);
        } catch {
          // Comparing resolved paths still blocks an exact configured match.
        }
        if (ledgerRealPath === forbiddenRealPath || this.ledgerDirectory === forbidden) {
          throw new OutboundLedgerError("ledger_directory_forbidden");
        }
      }
    } catch (err) {
      if (err instanceof OutboundLedgerError) throw err;
      throw new OutboundLedgerError("ledger_unavailable");
    }
  }

  recordPath(keyHash) {
    return path.join(this.ledgerDirectory, `${keyHash}.json`);
  }

  fsyncDirectory() {
    let descriptor;
    try {
      descriptor = this.fs.openSync(this.ledgerDirectory, "r");
      this.fs.fsyncSync(descriptor);
    } catch (err) {
      const unsupportedOnWindows = (
        process.platform === "win32"
        && ["EACCES", "EBADF", "EINVAL", "EISDIR", "ENOTSUP", "EPERM"].includes(err?.code)
      );
      if (!unsupportedOnWindows) throw err;
    } finally {
      if (descriptor !== undefined) this.fs.closeSync(descriptor);
    }
  }

  writeNewClaim(recordPath, record) {
    let descriptor;
    try {
      descriptor = this.fs.openSync(recordPath, "wx", 0o600);
      this.fs.writeFileSync(descriptor, `${JSON.stringify(record)}\n`, "utf8");
      this.fs.fsyncSync(descriptor);
      this.fs.closeSync(descriptor);
      descriptor = undefined;
      this.fsyncDirectory();
    } catch (err) {
      if (descriptor !== undefined) {
        try {
          this.fs.closeSync(descriptor);
        } catch {
          // The partial durable claim remains blocking.
        }
      }
      throw err;
    }
  }

  readRecord(recordPath, keyHash) {
    try {
      const stat = this.fs.statSync(recordPath);
      if (!stat.isFile() || stat.size < 2 || stat.size > 4096) {
        throw new OutboundLedgerError("ledger_record_invalid");
      }
      const serialized = this.fs.readFileSync(recordPath, "utf8");
      return validateRecord(JSON.parse(serialized), keyHash);
    } catch (err) {
      if (err instanceof OutboundLedgerError) throw err;
      throw new OutboundLedgerError("ledger_record_invalid");
    }
  }

  claim(idempotencyKey, fingerprint) {
    this.ensureAvailable();
    if (!validateIdempotencyKey(idempotencyKey) || !HASH_RE.test(fingerprint)) {
      throw new OutboundLedgerError("invalid_claim");
    }
    const keyHash = hashValue(idempotencyKey);
    const recordPath = this.recordPath(keyHash);
    const createdAt = this.now();
    const record = {
      schema_version: SCHEMA_VERSION,
      key_hash: keyHash,
      payload_fingerprint: fingerprint,
      state: "in_progress",
      created_at: createdAt,
    };

    try {
      this.writeNewClaim(recordPath, record);
      return { outcome: "claimed", record, recordPath };
    } catch (err) {
      if (err?.code !== "EEXIST") {
        throw new OutboundLedgerError("ledger_claim_failed");
      }
    }

    const existing = this.readRecord(recordPath, keyHash);
    if (existing.payload_fingerprint !== fingerprint) {
      return { outcome: "conflict", record: existing, recordPath };
    }
    if (existing.state === "sent") {
      return { outcome: "sent", record: existing, recordPath };
    }
    return { outcome: "blocked", record: existing, recordPath };
  }

  replaceRecord(recordPath, record) {
    const temporaryPath = path.join(
      this.ledgerDirectory,
      `.${record.key_hash}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`,
    );
    let descriptor;
    try {
      descriptor = this.fs.openSync(temporaryPath, "wx", 0o600);
      this.fs.writeFileSync(descriptor, `${JSON.stringify(record)}\n`, "utf8");
      this.fs.fsyncSync(descriptor);
      this.fs.closeSync(descriptor);
      descriptor = undefined;
      this.fs.renameSync(temporaryPath, recordPath);
      this.fsyncDirectory();
    } catch (err) {
      if (descriptor !== undefined) {
        try {
          this.fs.closeSync(descriptor);
        } catch {
          // The authoritative in-progress record remains blocking.
        }
      }
      try {
        this.fs.unlinkSync(temporaryPath);
      } catch {
        // A failed cleanup cannot make the original claim replayable.
      }
      throw new OutboundLedgerError("ledger_update_failed");
    }
  }

  markSent(claim, providerMessageId) {
    if (
      claim?.outcome !== "claimed"
      || typeof providerMessageId !== "string"
      || !providerMessageId.trim()
      || providerMessageId.length > 512
    ) {
      throw new OutboundLedgerError("invalid_sent_record");
    }
    const record = {
      ...claim.record,
      state: "sent",
      provider_message_id: providerMessageId.trim(),
      updated_at: this.now(),
    };
    this.replaceRecord(claim.recordPath, record);
    return record;
  }

  markUnknown(claim) {
    if (claim?.outcome !== "claimed") return;
    const record = {
      ...claim.record,
      state: "unknown",
      updated_at: this.now(),
    };
    delete record.provider_message_id;
    this.replaceRecord(claim.recordPath, record);
  }
}

module.exports = {
  OutboundLedger,
  OutboundLedgerError,
  hashValue,
  payloadFingerprint,
  validateIdempotencyKey,
  validateRecord,
};
