const COMMAND_SCHEMA = "rapp-base-command/1.0";
const BASE_SCHEMA = "rapp-static-api/1.0";
const PROFILE = "rapp-base/1.0";
const MAX_PREFILLED_ISSUE_URL = 7000;
const ID_RE = /^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/;
const COLLECTION_RE = /^[a-z][a-z0-9_-]{0,62}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const REVISION_RE = /^[0-9a-f]{64}$/;
const FORBIDDEN_PATH_PARTS = new Set(["__proto__", "prototype", "constructor"]);
const SYSTEM_FIELDS = new Set([
  "$schema",
  "schema",
  "id",
  "collection",
  "owner_id",
  "created_at",
  "updated_at",
  "deleted_at",
  "deleted",
  "prior_revision",
  "revision",
]);
const DEFAULT_LIMITS = Object.freeze({
  issue_body_bytes: 32768,
  command_bytes: 16384,
  json_depth: 8,
  json_nodes: 512,
  object_keys: 64,
  array_items: 100,
  string_bytes: 4096,
  fields_per_collection: 32,
  records_per_collection: 500,
  snapshot_items: 500,
  generated_collection_bytes: 2097152,
  issues_per_reconcile: 100,
  requests: 10000,
  events: 10000,
});
const MAX_LIMITS = Object.freeze({
  issue_body_bytes: 65536,
  command_bytes: 32768,
  json_depth: 12,
  json_nodes: 2048,
  object_keys: 256,
  array_items: 256,
  string_bytes: 8192,
  fields_per_collection: 64,
  records_per_collection: 2000,
  snapshot_items: 2000,
  generated_collection_bytes: 8388608,
  issues_per_reconcile: 1000,
  requests: 50000,
  events: 50000,
});

/** A structured SDK failure. */
export class RappBaseError extends Error {
  /**
   * @param {string} code
   * @param {string} message
   * @param {{status?: number, details?: unknown, cause?: unknown}} [options]
   */
  constructor(code, message, options = {}) {
    super(message, options.cause ? { cause: options.cause } : undefined);
    this.name = "RappBaseError";
    this.code = code;
    this.status = options.status ?? null;
    this.details = options.details ?? null;
  }
}

/**
 * @typedef {{field: string, op?: "eq"|"ne"|"lt"|"lte"|"gt"|"gte"|"contains"|"in"|"startsWith"|"endsWith"|"exists", value?: unknown}} Filter
 * @typedef {{field: string, direction?: "asc"|"desc"}} Sort
 * @typedef {{command: Record<string, unknown>, json: string, issueUrl: string, issueBody: string, title: string, requiresCopy: boolean}} PreparedCommand
 * @typedef {{schema: string, id: string, collection: string, revision: string, deleted: boolean, data?: Record<string, unknown>}} RappRecord
 * @typedef {{page: number, perPage: number, totalItems: number, totalPages: number, items: RappRecord[], snapshot: object, generation_sha256?: string}} RecordList
 */

/** Zero-dependency client for a bounded RAPP Base static API. */
export class RappBase {
  /**
   * @param {string|{baseUrl: string, repository?: string|{owner: string, name: string}, fetch?: typeof fetch, submit?: Function, limits?: Record<string, number>}} options
   */
  constructor(options) {
    const normalized = typeof options === "string" ? { baseUrl: options } : options;
    if (!normalized || typeof normalized.baseUrl !== "string") {
      throw new RappBaseError("invalid_options", "baseUrl is required");
    }
    this.baseUrl = normalizeBaseUrl(normalized.baseUrl);
    this._fetch = normalized.fetch ?? globalThis.fetch;
    if (typeof this._fetch !== "function") {
      throw new RappBaseError("missing_fetch", "a Fetch API implementation is required");
    }
    this.repository = normalizeRepository(
      normalized.repository ?? inferRepository(this.baseUrl),
    );
    this._submit = normalized.submit ?? null;
    this._limitOverrides = normalizeLimitOverrides(normalized.limits);
    this.limits = { ...DEFAULT_LIMITS, ...this._limitOverrides };
  }

  /** @param {string} name */
  collection(name) {
    assertSegment(name, COLLECTION_RE, "collection");
    return new CollectionClient(this, name);
  }

  /** Fetch the generated registry. */
  async getRegistry() {
    const value = await this._getJson("registry.json");
    if (
      value?.schema !== BASE_SCHEMA ||
      value?.profile !== PROFILE ||
      !Array.isArray(value.collections) ||
      !isPlainObject(value.capabilities?.limits)
    ) {
      throw new RappBaseError("protocol", "endpoint is not a RAPP Base registry");
    }
    this.limits = {
      ...validateRegistryLimits(value.capabilities.limits),
      ...this._limitOverrides,
    };
    if (!this.repository && value.repository) {
      this.repository = normalizeRepository(value.repository);
    }
    return value;
  }

  /**
   * Submit a prepared command. A supplied adapter takes precedence. The token
   * is used for this call only and is never retained or persisted.
   * @param {PreparedCommand} prepared
   * @param {{token?: string, adapter?: Function}} [options]
   */
  async submitCommand(prepared, options = {}) {
    validatePrepared(prepared);
    const adapter = options.adapter ?? this._submit;
    if (adapter !== null) {
      if (typeof adapter !== "function") {
        throw new RappBaseError("invalid_adapter", "submit adapter must be a function");
      }
      return adapter({
        body: prepared.issueBody,
        command: cloneJson(prepared.command),
        repository: this.repository ? { ...this.repository } : null,
        title: prepared.title,
      });
    }
    if (!this.repository) {
      throw new RappBaseError("missing_repository", "repository is required for submission");
    }
    if (typeof options.token !== "string" || options.token.length === 0) {
      throw new RappBaseError("missing_token", "a GitHub token is required for REST submission");
    }
    const endpoint = `https://api.github.com/repos/${encodeURIComponent(this.repository.owner)}/${encodeURIComponent(this.repository.name)}/issues`;
    let response;
    try {
      response = await this._fetch(endpoint, {
        method: "POST",
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${options.token}`,
          "Content-Type": "application/json",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({
          body: prepared.issueBody,
          title: prepared.title,
        }),
      });
    } catch (cause) {
      throw new RappBaseError("network", "GitHub issue submission failed", { cause });
    }
    return parseResponse(response, "GitHub issue submission failed");
  }

  /**
   * Poll a terminal command receipt. HTTP 404 means pending.
   * @param {string} commandId
   * @param {{intervalMs?: number, timeoutMs?: number, signal?: AbortSignal, onState?: (state: {status: "pending"|"terminal", receipt?: unknown}) => void}} [options]
   */
  async pollReceipt(commandId, options = {}) {
    assertUuid(commandId);
    const intervalMs = finiteInteger(options.intervalMs ?? 2000, 0, 60000, "intervalMs");
    const timeoutMs = finiteInteger(options.timeoutMs ?? 120000, 1, 3600000, "timeoutMs");
    if (options.signal?.aborted) {
      throw new RappBaseError("aborted", "receipt polling was aborted");
    }
    const controller = new AbortController();
    let timedOut = false;
    let attempt = 0;
    let rejectBoundary;
    const onCallerAbort = () => {
      controller.abort();
      rejectBoundary?.(new RappBaseError("aborted", "receipt polling was aborted"));
    };
    let timeout;
    const deadline = new Promise((_, reject) => {
      rejectBoundary = reject;
      timeout = setTimeout(() => {
        timedOut = true;
        controller.abort();
        reject(new RappBaseError("timeout", "receipt was not published before timeout"));
      }, timeoutMs);
    });
    options.signal?.addEventListener("abort", onCallerAbort, { once: true });
    try {
      for (;;) {
        try {
          attempt += 1;
          const receipt = await Promise.race([
            this._getJson(
              `api/v1/receipts/commands/${encodeURIComponent(commandId)}.json` +
                `?rapp_base_poll=${Date.now()}-${attempt}`,
              { signal: controller.signal, cache: "no-store" },
            ),
            deadline,
          ]);
          validateReceipt(receipt, commandId);
          options.onState?.({ status: "terminal", receipt });
          return receipt;
        } catch (error) {
          if (options.signal?.aborted) {
            throw new RappBaseError("aborted", "receipt polling was aborted");
          }
          if (timedOut) {
            throw new RappBaseError("timeout", "receipt was not published before timeout");
          }
          if (!(error instanceof RappBaseError) || error.status !== 404) {
            throw error;
          }
        }
        options.onState?.({ status: "pending" });
        await Promise.race([delay(intervalMs, controller.signal), deadline]);
      }
    } catch (error) {
      if (options.signal?.aborted) {
        throw new RappBaseError("aborted", "receipt polling was aborted");
      }
      if (timedOut) {
        throw new RappBaseError("timeout", "receipt was not published before timeout");
      }
      throw error;
    } finally {
      clearTimeout(timeout);
      options.signal?.removeEventListener("abort", onCallerAbort);
    }
  }

  async _getJson(relative, init = {}) {
    const url = new URL(relative, this.baseUrl).href;
    let response;
    try {
      response = await this._fetch(url, {
        ...init,
        headers: { Accept: "application/json", ...(init.headers ?? {}) },
      });
    } catch (cause) {
      throw new RappBaseError("network", `request failed: ${url}`, { cause });
    }
    return parseResponse(response, `request failed: ${url}`);
  }
}

class CollectionClient {
  constructor(client, name) {
    this.client = client;
    this.name = name;
  }

  /** @param {string} id @returns {Promise<RappRecord>} */
  async getOne(id) {
    assertSegment(id, ID_RE, "record id");
    const record = await this.client._getJson(
      `api/v1/collections/${encodeURIComponent(this.name)}/records/${encodeURIComponent(id)}.json`,
    );
    validateRecord(record, this.name, id, this.client.limits);
    return record;
  }

  /**
   * Apply structured client-side query operations to the complete bounded snapshot.
   * @param {number} [page]
   * @param {number} [perPage]
   * @param {{filter?: Filter|Filter[], sort?: Sort|Sort[]}} [options]
   * @returns {Promise<RecordList>}
   */
  async getList(page = 1, perPage = 50, options = {}) {
    page = finiteInteger(page, 1, Number.MAX_SAFE_INTEGER, "page");
    perPage = finiteInteger(
      perPage,
      1,
      this.client.limits.snapshot_items,
      "perPage",
    );
    const snapshot = await this._getSnapshot();
    let items = applyFilters(snapshot.items, options.filter);
    items = applySort(items, options.sort);
    const totalItems = items.length;
    const totalPages = Math.ceil(totalItems / perPage);
    const start = (page - 1) * perPage;
    return {
      page,
      perPage,
      totalItems,
      totalPages,
      items: items.slice(start, start + perPage),
      snapshot: snapshot.snapshot,
      generation_sha256: snapshot.generation_sha256,
    };
  }

  /** @param {{filter?: Filter|Filter[], sort?: Sort|Sort[]}} [options] @returns {Promise<RappRecord[]>} */
  async getFullList(options = {}) {
    const snapshot = await this._getSnapshot();
    return applySort(applyFilters(snapshot.items, options.filter), options.sort);
  }

  /** @param {Record<string, unknown>} data @param {{commandId?: string}} [options] @returns {PreparedCommand} */
  prepareCreate(data, options = {}) {
    return this._prepare("create", { data }, options);
  }

  /**
   * @param {string} recordId
   * @param {string} ifRevision
   * @param {Record<string, unknown>} data
   * @param {{commandId?: string}} [options]
   * @returns {PreparedCommand}
   */
  prepareUpdate(recordId, ifRevision, data, options = {}) {
    assertSegment(recordId, ID_RE, "record id");
    assertRevision(ifRevision);
    return this._prepare(
      "update",
      { record_id: recordId, if_revision: ifRevision, data },
      options,
    );
  }

  /**
   * @param {string} recordId
   * @param {string} ifRevision
   * @param {{commandId?: string}} [options]
   * @returns {PreparedCommand}
   */
  prepareDelete(recordId, ifRevision, options = {}) {
    assertSegment(recordId, ID_RE, "record id");
    assertRevision(ifRevision);
    return this._prepare(
      "delete",
      { record_id: recordId, if_revision: ifRevision },
      options,
    );
  }

  async _getSnapshot() {
    const value = await this.client._getJson(
      `api/v1/collections/${encodeURIComponent(this.name)}/records.json`,
    );
    if (
      value?.schema !== "rapp-base-record-list/1.0" ||
      !Array.isArray(value.items) ||
      value.snapshot?.complete !== true ||
      value.totalItems !== value.items.length ||
      !Number.isSafeInteger(value.page) ||
      !Number.isSafeInteger(value.perPage) ||
      !Number.isSafeInteger(value.totalPages)
    ) {
      throw new RappBaseError("protocol", "collection snapshot is invalid or incomplete");
    }
    value.items.forEach((record) =>
      validateRecord(record, this.name, null, this.client.limits)
    );
    return value;
  }

  _prepare(operation, fields, options) {
    if (!this.client.repository) {
      throw new RappBaseError(
        "missing_repository",
        "repository is required to prepare an Issue URL",
      );
    }
    const commandId = options.commandId ?? globalThis.crypto?.randomUUID?.();
    if (!commandId) {
      throw new RappBaseError(
        "missing_uuid",
        "crypto.randomUUID is unavailable; provide commandId",
      );
    }
    assertUuid(commandId);
    const command = cloneJson({
      schema: COMMAND_SCHEMA,
      command_id: commandId,
      operation,
      collection: this.name,
      ...fields,
    }, 0, { nodes: 0, keys: 0 }, this.client.limits);
    if ("data" in command) {
      for (const key of Object.keys(command.data)) {
        if (SYSTEM_FIELDS.has(key)) {
          throw new RappBaseError("reserved_field", `${key} is a reserved field`);
        }
      }
    }
    const json = JSON.stringify(command, null, 2);
    if (new TextEncoder().encode(json).length > this.client.limits.command_bytes) {
      throw new RappBaseError("command_too_large", "command exceeds the configured byte limit");
    }
    const title = `[RAPP Base] ${operation} ${this.name}`;
    const issueBody = `### Command\n\n\`\`\`json\n${json}\n\`\`\``;
    if (
      new TextEncoder().encode(issueBody).length >
      this.client.limits.issue_body_bytes
    ) {
      throw new RappBaseError(
        "body_too_large",
        "Issue body exceeds the configured byte limit",
      );
    }
    const params = new URLSearchParams({
      command: json,
      template: "rapp-base-command.yml",
      title,
    });
    const { owner, name } = this.client.repository;
    const issueBase = `https://github.com/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/issues/new`;
    const prefilledUrl = `${issueBase}?${params}`;
    const requiresCopy = prefilledUrl.length > MAX_PREFILLED_ISSUE_URL;
    const issueUrl = requiresCopy
      ? `${issueBase}?${new URLSearchParams({ template: "rapp-base-command.yml" })}`
      : prefilledUrl;
    return { command, issueBody, issueUrl, json, requiresCopy, title };
  }
}

function normalizeBaseUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch (cause) {
    throw new RappBaseError("invalid_base_url", "baseUrl must be an absolute URL", { cause });
  }
  if (!["https:", "http:"].includes(url.protocol)) {
    throw new RappBaseError("invalid_base_url", "baseUrl must use HTTP or HTTPS");
  }
  if (url.username || url.password) {
    throw new RappBaseError("invalid_base_url", "baseUrl cannot contain credentials");
  }
  url.hash = "";
  url.search = "";
  if (!url.pathname.endsWith("/")) url.pathname += "/";
  return url.href;
}

function inferRepository(baseUrl) {
  const url = new URL(baseUrl);
  if (url.hostname === "raw.githubusercontent.com") {
    const [owner, name] = url.pathname.split("/").filter(Boolean);
    return owner && name ? { owner, name } : null;
  }
  if (url.hostname.endsWith(".github.io")) {
    const owner = url.hostname.slice(0, -".github.io".length);
    const [name] = url.pathname.split("/").filter(Boolean);
    return owner && name ? { owner, name } : null;
  }
  return null;
}

function normalizeRepository(value) {
  if (value == null) return null;
  if (typeof value === "string") {
    const parts = value.split("/");
    if (parts.length !== 2) {
      throw new RappBaseError("invalid_repository", "repository must be owner/name");
    }
    value = { owner: parts[0], name: parts[1] };
  }
  if (
    typeof value !== "object" ||
    !/^[A-Za-z0-9_.-]{1,100}$/.test(value.owner) ||
    !/^[A-Za-z0-9_.-]{1,100}$/.test(value.name) ||
    [".", ".."].includes(value.owner) ||
    [".", ".."].includes(value.name)
  ) {
    throw new RappBaseError("invalid_repository", "repository is invalid");
  }
  return { owner: value.owner, name: value.name };
}

async function parseResponse(response, message) {
  if (!response || typeof response.ok !== "boolean") {
    throw new RappBaseError("invalid_response", "fetch returned an invalid response");
  }
  let text;
  try {
    text = await response.text();
  } catch (cause) {
    throw new RappBaseError("network", message, { status: response.status, cause });
  }
  let value = null;
  if (text) {
    try {
      value = JSON.parse(text);
    } catch (cause) {
      throw new RappBaseError("invalid_json", "response was not valid JSON", {
        status: response.status,
        details: text.slice(0, 1000),
        cause,
      });
    }
  }
  if (!response.ok) {
    throw new RappBaseError("http", message, {
      status: response.status,
      details: value,
    });
  }
  return value;
}

function validateRecord(record, collection, requestedId = null, limits = DEFAULT_LIMITS) {
  if (!isPlainObject(record) || record.collection !== collection) {
    throw new RappBaseError("protocol", "record document is invalid");
  }
  if (
    typeof record.id !== "string" ||
    !ID_RE.test(record.id) ||
    record.id === "." ||
    record.id === ".."
  ) {
    throw new RappBaseError("protocol", "record identity is invalid");
  }
  if (requestedId !== null && record.id !== requestedId) {
    throw new RappBaseError("protocol", "record identity does not match the request");
  }
  if (!REVISION_RE.test(record.revision) || !isOwnerId(record.owner_id)) {
    throw new RappBaseError("protocol", "record document is invalid");
  }
  assertTimestamp(record.created_at);
  assertTimestamp(record.updated_at);
  if (record.schema === "rapp-base-record/1.0") {
    assertExactKeys(record, [
      "schema", "collection", "created_at", "data", "deleted", "id",
      "owner_id", "revision", "updated_at",
    ], "record");
    if (record.deleted !== false || !isPlainObject(record.data)) {
      throw new RappBaseError("protocol", "live record document is invalid");
    }
    cloneJson(record.data, 0, { nodes: 0, keys: 0 }, limits);
    return;
  }
  if (record.schema === "rapp-base-tombstone/1.0") {
    assertExactKeys(record, [
      "schema", "collection", "created_at", "deleted", "deleted_at", "id",
      "owner_id", "prior_revision", "revision", "updated_at",
    ], "tombstone");
    if (
      record.deleted !== true ||
      !REVISION_RE.test(record.prior_revision) ||
      record.deleted_at !== record.updated_at
    ) {
      throw new RappBaseError("protocol", "tombstone document is invalid");
    }
    assertTimestamp(record.deleted_at);
    return;
  }
  throw new RappBaseError("protocol", "record schema is invalid");
}

function applyFilters(items, input) {
  if (input == null) return [...items];
  const filters = Array.isArray(input) ? input : [input];
  filters.forEach(validateFilter);
  return items.filter((item) =>
    filters.every((filter) => matchesFilter(getPath(item, filter.field), filter)),
  );
}

function validateFilter(filter) {
  if (!filter || typeof filter !== "object" || Array.isArray(filter)) {
    throw new RappBaseError("invalid_filter", "filter must be an object");
  }
  validatePath(filter.field);
  const op = filter.op ?? "eq";
  if (!["eq", "ne", "lt", "lte", "gt", "gte", "contains", "in", "startsWith", "endsWith", "exists"].includes(op)) {
    throw new RappBaseError("invalid_filter", `unsupported filter operator: ${op}`);
  }
  if (op === "in" && !Array.isArray(filter.value)) {
    throw new RappBaseError("invalid_filter", "in filter value must be an array");
  }
  if (op === "exists" && typeof filter.value !== "boolean") {
    throw new RappBaseError("invalid_filter", "exists filter value must be boolean");
  }
}

function matchesFilter(actual, filter) {
  const op = filter.op ?? "eq";
  const expected = filter.value;
  switch (op) {
    case "eq": return equalValue(actual, expected);
    case "ne": return !equalValue(actual, expected);
    case "lt": return relationalCompare(actual, expected, (value) => value < 0);
    case "lte": return relationalCompare(actual, expected, (value) => value <= 0);
    case "gt": return relationalCompare(actual, expected, (value) => value > 0);
    case "gte": return relationalCompare(actual, expected, (value) => value >= 0);
    case "contains":
      return (typeof actual === "string" && typeof expected === "string" && actual.includes(expected)) ||
        (Array.isArray(actual) && actual.some((item) => equalValue(item, expected)));
    case "in": return expected.some((item) => equalValue(actual, item));
    case "startsWith": return typeof actual === "string" && typeof expected === "string" && actual.startsWith(expected);
    case "endsWith": return typeof actual === "string" && typeof expected === "string" && actual.endsWith(expected);
    case "exists": return expected ? actual !== undefined : actual === undefined;
    default: return false;
  }
}

function applySort(items, input) {
  if (input == null) return [...items];
  const sorts = Array.isArray(input) ? input : [input];
  sorts.forEach((sort) => {
    if (!sort || typeof sort !== "object") {
      throw new RappBaseError("invalid_sort", "sort must be an object");
    }
    validatePath(sort.field);
    if (!["asc", "desc"].includes(sort.direction ?? "asc")) {
      throw new RappBaseError("invalid_sort", "sort direction must be asc or desc");
    }
  });
  return items
    .map((value, index) => ({ value, index }))
    .sort((left, right) => {
      for (const sort of sorts) {
        const direction = (sort.direction ?? "asc") === "asc" ? 1 : -1;
        const a = getPath(left.value, sort.field);
        const b = getPath(right.value, sort.field);
        const compared = compareValues(a, b);
        if (compared !== 0) return compared * direction;
      }
      return left.index - right.index;
    })
    .map(({ value }) => value);
}

function compareValues(a, b) {
  if (a === undefined || a === null) return b === undefined || b === null ? 0 : 1;
  if (b === undefined || b === null) return -1;
  if (typeof a === "string" && typeof b === "string") return compareCodePoints(a, b);
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  const rank = (value) => {
    if (typeof value === "number") return 0;
    if (typeof value === "string") return 1;
    if (typeof value === "boolean") return 2;
    if (Array.isArray(value)) return 3;
    if (isPlainObject(value)) return 4;
    return 5;
  };
  const rankDifference = rank(a) - rank(b);
  if (rankDifference !== 0) return rankDifference;
  return compareCodePoints(canonicalJson(a), canonicalJson(b));
}

function validatePath(path) {
  if (typeof path !== "string" || path.length === 0 || path.length > 200) {
    throw new RappBaseError("invalid_path", "query field path is invalid");
  }
  const parts = path.split(".");
  if (
    parts.length > 8 ||
    parts.some((part) => !/^[A-Za-z0-9_$-]+$/.test(part) || FORBIDDEN_PATH_PARTS.has(part))
  ) {
    throw new RappBaseError("invalid_path", "query field path is unsafe");
  }
}

function getPath(value, path) {
  let current = value;
  for (const part of path.split(".")) {
    if (
      current === null ||
      typeof current !== "object" ||
      !Object.prototype.hasOwnProperty.call(current, part)
    ) return undefined;
    current = current[part];
  }
  return current;
}

function equalValue(a, b) {
  if (Object.is(a, b)) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, index) => equalValue(item, b[index]));
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const left = Object.keys(a).sort(compareCodePoints);
    const right = Object.keys(b).sort(compareCodePoints);
    return left.length === right.length &&
      left.every((key, index) => key === right[index] && equalValue(a[key], b[key]));
  }
  return false;
}

function cloneJson(
  value,
  depth = 0,
  budget = { nodes: 0, keys: 0 },
  limits = DEFAULT_LIMITS,
) {
  if (depth >= limits.json_depth) {
    throw new RappBaseError("invalid_data", "data is nested too deeply");
  }
  budget.nodes += 1;
  if (budget.nodes > limits.json_nodes) {
    throw new RappBaseError("invalid_data", "data has too many JSON nodes");
  }
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "string") {
    if (/[\u0000-\u001f\u007f-\u009f]/.test(value)) {
      throw new RappBaseError("invalid_data", "control characters are forbidden");
    }
    if (new TextEncoder().encode(value).length > limits.string_bytes) {
      throw new RappBaseError("invalid_data", "string exceeds the configured byte limit");
    }
    return value;
  }
  if (
    typeof value === "number" &&
    Number.isFinite(value) &&
    Math.abs(value) <= Number.MAX_SAFE_INTEGER
  ) return Object.is(value, -0) ? 0 : value;
  if (Array.isArray(value)) {
    if (value.length > limits.array_items) {
      throw new RappBaseError("invalid_data", "array is too large");
    }
    return value.map((item) => cloneJson(item, depth + 1, budget, limits));
  }
  if (isPlainObject(value)) {
    const result = {};
    budget.keys += Object.keys(value).length;
    if (budget.keys > limits.object_keys) {
      throw new RappBaseError("invalid_data", "data has too many object keys");
    }
    for (const [key, item] of Object.entries(value)) {
      if (
        /[\u0000-\u001f\u007f-\u009f]/.test(key) ||
        FORBIDDEN_PATH_PARTS.has(key)
      ) {
        throw new RappBaseError("invalid_data", "unsafe object key is forbidden");
      }
      result[key] = cloneJson(item, depth + 1, budget, limits);
    }
    return result;
  }
  throw new RappBaseError("invalid_data", "data must contain only finite JSON values");
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function normalizeLimitOverrides(value) {
  if (value == null) return {};
  if (!isPlainObject(value)) {
    throw new RappBaseError("invalid_options", "limits must be an object");
  }
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    if (!Object.hasOwn(MAX_LIMITS, key)) {
      throw new RappBaseError("invalid_options", `unknown limit: ${key}`);
    }
    result[key] = finiteInteger(item, 1, MAX_LIMITS[key], `limits.${key}`);
  }
  return result;
}

function validateRegistryLimits(value) {
  const result = {};
  for (const key of Object.keys(MAX_LIMITS)) {
    if (!Object.hasOwn(value, key)) {
      throw new RappBaseError("protocol", `registry is missing limit ${key}`);
    }
    try {
      result[key] = finiteInteger(value[key], 1, MAX_LIMITS[key], `limits.${key}`);
    } catch (cause) {
      throw new RappBaseError("protocol", `registry limit ${key} is invalid`, { cause });
    }
  }
  if (Object.keys(value).some((key) => !Object.hasOwn(MAX_LIMITS, key))) {
    throw new RappBaseError("protocol", "registry contains an unknown limit");
  }
  return result;
}

function assertExactKeys(value, expected, context) {
  const actual = Object.keys(value).sort(compareCodePoints);
  const wanted = [...expected].sort(compareCodePoints);
  if (!equalValue(actual, wanted)) {
    throw new RappBaseError("protocol", `${context} has invalid keys`);
  }
}

function isOwnerId(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function assertTimestamp(value) {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value) ||
    !Number.isFinite(Date.parse(value))
  ) {
    throw new RappBaseError("protocol", "timestamp is invalid");
  }
}

function validateReceipt(receipt, requestedCommandId) {
  if (!isPlainObject(receipt)) {
    throw new RappBaseError("protocol", "receipt document is invalid");
  }
  assertExactKeys(receipt, [
    "schema", "actor_id", "code", "command_id", "duplicate_of", "event",
    "issue", "message", "occurred_at", "receipt_id", "record", "request_hash",
    "status",
  ], "receipt");
  if (
    receipt.schema !== "rapp-base-receipt/1.0" ||
    receipt.command_id !== requestedCommandId ||
    !REVISION_RE.test(receipt.receipt_id) ||
    !REVISION_RE.test(receipt.request_hash) ||
    !Number.isSafeInteger(receipt.actor_id) ||
    receipt.actor_id < 1 ||
    !["applied", "rejected", "noop"].includes(receipt.status) ||
    typeof receipt.code !== "string" ||
    typeof receipt.message !== "string"
  ) {
    throw new RappBaseError("protocol", "receipt document is invalid");
  }
  assertTimestamp(receipt.occurred_at);
  if (!isPlainObject(receipt.issue)) {
    throw new RappBaseError("protocol", "receipt Issue identity is invalid");
  }
  assertExactKeys(receipt.issue, ["id", "node_id", "number", "title"], "receipt Issue");
  if (
    !Number.isSafeInteger(receipt.issue.id) ||
    receipt.issue.id < 1 ||
    !Number.isSafeInteger(receipt.issue.number) ||
    receipt.issue.number < 1 ||
    typeof receipt.issue.node_id !== "string" ||
    typeof receipt.issue.title !== "string"
  ) {
    throw new RappBaseError("protocol", "receipt Issue identity is invalid");
  }
  if (receipt.status === "applied") {
    if (!isPlainObject(receipt.event) || !isPlainObject(receipt.record)) {
      throw new RappBaseError("protocol", "applied receipt lacks result references");
    }
    assertExactKeys(receipt.event, ["hash", "path", "sequence"], "receipt event");
    assertExactKeys(
      receipt.record,
      ["collection", "deleted", "id", "revision"],
      "receipt record",
    );
    if (
      !REVISION_RE.test(receipt.event.hash) ||
      !Number.isSafeInteger(receipt.event.sequence) ||
      receipt.event.sequence < 1 ||
      receipt.event.path !==
        `${String(receipt.event.sequence).padStart(8, "0")}-${receipt.event.hash.slice(0, 12)}.json` ||
      !COLLECTION_RE.test(receipt.record.collection) ||
      !ID_RE.test(receipt.record.id) ||
      typeof receipt.record.deleted !== "boolean" ||
      !REVISION_RE.test(receipt.record.revision)
    ) {
      throw new RappBaseError("protocol", "applied receipt result is invalid");
    }
  } else if (receipt.event !== null || receipt.record !== null) {
    throw new RappBaseError("protocol", "non-applied receipt has result references");
  }
  if (receipt.duplicate_of !== null) {
    if (!isPlainObject(receipt.duplicate_of)) {
      throw new RappBaseError("protocol", "receipt duplicate reference is invalid");
    }
    assertExactKeys(
      receipt.duplicate_of,
      ["issue_id", "request_hash"],
      "receipt duplicate reference",
    );
    if (
      !Number.isSafeInteger(receipt.duplicate_of.issue_id) ||
      receipt.duplicate_of.issue_id < 1 ||
      !REVISION_RE.test(receipt.duplicate_of.request_hash)
    ) {
      throw new RappBaseError("protocol", "receipt duplicate reference is invalid");
    }
  }
}

function compareCodePoints(left, right) {
  const a = Array.from(left, (value) => value.codePointAt(0));
  const b = Array.from(right, (value) => value.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1;
  }
  return a.length - b.length;
}

function canonicalJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (isPlainObject(value)) {
    return `{${Object.keys(value)
      .sort(compareCodePoints)
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function relationalCompare(actual, expected, predicate) {
  if (typeof actual !== typeof expected) return false;
  if (typeof actual === "number") {
    return Number.isFinite(actual) && Number.isFinite(expected) &&
      predicate(actual - expected);
  }
  if (typeof actual === "string") {
    return predicate(compareCodePoints(actual, expected));
  }
  return false;
}

function assertSegment(value, pattern, field) {
  if (typeof value !== "string" || !pattern.test(value) || value === "." || value === "..") {
    throw new RappBaseError("invalid_path", `${field} is not a safe URL segment`);
  }
}

function assertUuid(value) {
  if (typeof value !== "string" || !UUID_RE.test(value) || /^0{8}-0{4}-0{4}-0{4}-0{12}$/.test(value)) {
    throw new RappBaseError("invalid_command_id", "commandId must be a lowercase UUID");
  }
}

function assertRevision(value) {
  if (typeof value !== "string" || !REVISION_RE.test(value)) {
    throw new RappBaseError("invalid_revision", "ifRevision must be a full SHA-256");
  }
}

function finiteInteger(value, minimum, maximum, name) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new RappBaseError("invalid_argument", `${name} is out of range`);
  }
  return value;
}

function validatePrepared(value) {
  if (
    !value ||
    typeof value !== "object" ||
    typeof value.issueBody !== "string" ||
    typeof value.issueUrl !== "string" ||
    typeof value.json !== "string" ||
    typeof value.title !== "string" ||
    typeof value.requiresCopy !== "boolean" ||
    !value.command ||
    value.command.schema !== COMMAND_SCHEMA ||
    !["create", "update", "delete"].includes(value.command.operation) ||
    typeof value.command.collection !== "string"
  ) {
    throw new RappBaseError("invalid_command", "prepared command is invalid");
  }
  const expectedJson = JSON.stringify(value.command, null, 2);
  const expectedTitle =
    `[RAPP Base] ${value.command.operation} ${value.command.collection}`;
  if (
    value.json !== expectedJson ||
    value.title !== expectedTitle ||
    value.issueBody !== `### Command\n\n\`\`\`json\n${expectedJson}\n\`\`\``
  ) {
    throw new RappBaseError(
      "invalid_command",
      "prepared command is not an official routable Issue",
    );
  }
}

function delay(milliseconds, signal) {
  if (signal?.aborted) {
    return Promise.reject(new RappBaseError("aborted", "receipt polling was aborted"));
  }
  if (milliseconds === 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => signal?.removeEventListener("abort", onAbort);
    const timer = setTimeout(() => {
      cleanup();
      resolve();
    }, milliseconds);
    const onAbort = () => {
      clearTimeout(timer);
      cleanup();
      reject(new RappBaseError("aborted", "receipt polling was aborted"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}
