import assert from "node:assert/strict";
import test from "node:test";

import { RappBase, RappBaseError } from "./rapp-base.js";

const revision = "a".repeat(64);
const timestamp = "2026-07-18T19:00:00Z";
const registryLimits = {
  issue_body_bytes: 32768,
  command_bytes: 16384,
  json_depth: 8,
  json_nodes: 512,
  object_keys: 64,
  array_items: 100,
  string_bytes: 8192,
  fields_per_collection: 32,
  records_per_collection: 500,
  snapshot_items: 500,
  generated_collection_bytes: 2097152,
  issues_per_reconcile: 100,
  requests: 10000,
  events: 10000,
};
const records = [
  {
    schema: "rapp-base-record/1.0",
    collection: "resources",
    created_at: timestamp,
    deleted: false,
    id: "one",
    owner_id: 1,
    revision,
    updated_at: timestamp,
    data: { title: "Zulu", rating: 3, topics: ["python"] },
  },
  {
    schema: "rapp-base-record/1.0",
    collection: "resources",
    created_at: timestamp,
    deleted: false,
    id: "two",
    owner_id: 1,
    revision: "b".repeat(64),
    updated_at: timestamp,
    data: { title: "Alpha", rating: 5, topics: ["github", "python"] },
  },
  {
    schema: "rapp-base-record/1.0",
    collection: "resources",
    created_at: timestamp,
    deleted: false,
    id: "three",
    owner_id: 1,
    revision: "c".repeat(64),
    updated_at: timestamp,
    data: { title: "Beta", rating: 4, topics: ["rust"] },
  },
];

function snapshot(items = records) {
  return {
    schema: "rapp-base-record-list/1.0",
    generation_sha256: "d".repeat(64),
    items,
    page: 1,
    perPage: 500,
    snapshot: { complete: true },
    totalItems: items.length,
    totalPages: items.length ? 1 : 0,
  };
}

function receipt(commandId, overrides = {}) {
  return {
    schema: "rapp-base-receipt/1.0",
    actor_id: 7,
    code: "forbidden",
    command_id: commandId,
    duplicate_of: null,
    event: null,
    issue: {
      id: 10,
      node_id: "I_ten",
      number: 10,
      title: "[RAPP Base] update resources",
    },
    message: "the GitHub actor is not authorized for this operation",
    occurred_at: timestamp,
    receipt_id: "e".repeat(64),
    record: null,
    request_hash: "f".repeat(64),
    status: "rejected",
    ...overrides,
  };
}

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() {
      return body === null ? "" : JSON.stringify(body);
    },
  };
}

test("queries bounded snapshots with structured filters, sorting, and pagination", async () => {
  const fetch = async () =>
    response(200, snapshot());
  const client = new RappBase({
    baseUrl: "https://example.test/base/",
    repository: "owner/repo",
    fetch,
  });
  const page = await client.collection("resources").getList(1, 1, {
    filter: [
      { field: "data.rating", op: "gte", value: 4 },
      { field: "data.topics", op: "contains", value: "python" },
    ],
    sort: { field: "data.title", direction: "asc" },
  });
  assert.equal(page.totalItems, 1);
  assert.equal(page.items[0].id, "two");
});

test("getFullList sorts without evaluating expressions", async () => {
  const client = new RappBase({
    baseUrl: "https://example.test/",
    repository: "owner/repo",
    fetch: async () => response(200, snapshot()),
  });
  const result = await client.collection("resources").getFullList({
    sort: { field: "data.title", direction: "asc" },
  });
  assert.deepEqual(result.map((item) => item.id), ["two", "three", "one"]);
  await assert.rejects(
    () =>
      client.collection("resources").getFullList({
        sort: { field: "__proto__.polluted" },
      }),
    (error) => error instanceof RappBaseError && error.code === "invalid_path",
  );
});

test("prepares strict create, update, and delete drafts", () => {
  const client = new RappBase({
    baseUrl: "https://raw.githubusercontent.com/owner/repo/main/",
    fetch: async () => response(404, {}),
  });
  const collection = client.collection("resources");
  const create = collection.prepareCreate(
    { title: "Example" },
    { commandId: "123e4567-e89b-42d3-a456-426614174000" },
  );
  assert.equal(create.command.operation, "create");
  assert.match(create.issueUrl, /template=rapp-base-command\.yml/);
  assert.match(create.issueBody, /^### Command\n\n```json\n/);
  const update = collection.prepareUpdate(
    "one",
    revision,
    { title: "Changed" },
    { commandId: "123e4567-e89b-42d3-a456-426614174001" },
  );
  assert.equal(update.command.if_revision, revision);
  const remove = collection.prepareDelete(
    "one",
    revision,
    { commandId: "123e4567-e89b-42d3-a456-426614174002" },
  );
  assert.equal("data" in remove.command, false);
});

test("rejects reserved fields and invalid HTTP responses clearly", async () => {
  const client = new RappBase({
    baseUrl: "https://example.test/",
    repository: "owner/repo",
    fetch: async () => response(500, { message: "failed" }),
  });
  assert.throws(
    () =>
      client.collection("resources").prepareCreate(
        { owner_id: 7 },
        { commandId: "123e4567-e89b-42d3-a456-426614174000" },
      ),
    (error) => error instanceof RappBaseError && error.code === "reserved_field",
  );
  assert.throws(
    () =>
      client.collection("resources").prepareCreate(
        { title: "line\nbreak" },
        { commandId: "123e4567-e89b-42d3-a456-426614174000" },
      ),
    (error) => error instanceof RappBaseError && error.code === "invalid_data",
  );
  assert.throws(
    () =>
      client.collection("resources").prepareCreate(
        Object.fromEntries(
          Array.from({ length: 65 }, (_, index) => [`field-${index}`, index]),
        ),
        { commandId: "123e4567-e89b-42d3-a456-426614174000" },
      ),
    (error) => error instanceof RappBaseError && error.code === "invalid_data",
  );
  await assert.rejects(
    () => client.collection("resources").getOne("one"),
    (error) =>
      error instanceof RappBaseError &&
      error.code === "http" &&
      error.status === 500,
  );
});

test("polls 404 pending state and returns a terminal receipt", async () => {
  let calls = 0;
  const states = [];
  const requests = [];
  const commandId = "123e4567-e89b-42d3-a456-426614174000";
  const client = new RappBase({
    baseUrl: "https://example.test/",
    repository: "owner/repo",
    fetch: async (url, init) => {
      requests.push({ url, init });
      calls += 1;
      return calls === 1
        ? response(404, { message: "not found" })
        : response(200, receipt(commandId));
    },
  });
  const result = await client.pollReceipt(
    commandId,
    { intervalMs: 0, timeoutMs: 1000, onState: (state) => states.push(state.status) },
  );
  assert.equal(result.status, "rejected");
  assert.deepEqual(states, ["pending", "terminal"]);
  assert.ok(requests.every(({ url }) => url.includes("rapp_base_poll=")));
  assert.ok(requests.every(({ init }) => init.cache === "no-store"));
});

test("uses an injected submit adapter without retaining a token", async () => {
  let submitted;
  const client = new RappBase({
    baseUrl: "https://example.test/",
    repository: "owner/repo",
    fetch: async () => response(500, {}),
    submit: async (value) => {
      submitted = value;
      return { number: 9 };
    },
  });
  const draft = client.collection("resources").prepareDelete(
    "one",
    revision,
    { commandId: "123e4567-e89b-42d3-a456-426614174000" },
  );
  const result = await client.submitCommand(draft, { token: "not-used" });
  assert.equal(result.number, 9);
  assert.equal(submitted.command.operation, "delete");
  assert.equal(Object.hasOwn(client, "token"), false);
});

test("REST submission relies on the fixed title and body, not labels", async () => {
  let request;
  const client = new RappBase({
    baseUrl: "https://example.test/",
    repository: "owner/repo",
    fetch: async (url, init) => {
      request = { url, init };
      return response(201, { number: 12 });
    },
  });
  const draft = client.collection("resources").prepareCreate(
    { title: "Example" },
    { commandId: "123e4567-e89b-42d3-a456-426614174010" },
  );
  await client.submitCommand(draft, { token: "ephemeral" });
  const body = JSON.parse(request.init.body);
  assert.match(body.title, /^\[RAPP Base\]/);
  assert.match(body.body, /^### Command\n\n```json\n/);
  assert.equal("labels" in body, false);
  await assert.rejects(
    () => client.submitCommand(
      { ...draft, title: "unroutable" },
      { token: "ephemeral" },
    ),
    (error) => error instanceof RappBaseError && error.code === "invalid_command",
  );
});

test("long commands return a template-only URL and raw field JSON", () => {
  const client = new RappBase({
    baseUrl: "https://example.test/",
    repository: "owner/repo",
    limits: { string_bytes: 8192, command_bytes: 32768 },
    fetch: async () => response(404, {}),
  });
  const draft = client.collection("resources").prepareCreate(
    { summary: "x".repeat(7500) },
    { commandId: "123e4567-e89b-42d3-a456-426614174011" },
  );
  assert.equal(draft.requiresCopy, true);
  assert.match(draft.issueUrl, /\?template=rapp-base-command\.yml$/);
  assert.deepEqual(JSON.parse(draft.json), draft.command);
  assert.equal(
    draft.issueBody,
    `### Command\n\n\`\`\`json\n${draft.json}\n\`\`\``,
  );
  assert.doesNotMatch(draft.json, /^### Command/);
  assert.ok(draft.issueUrl.length < 500);
});

test("registry limits replace conservative defaults while overrides win", async () => {
  const registry = {
    schema: "rapp-static-api/1.0",
    profile: "rapp-base/1.0",
    capabilities: { limits: registryLimits },
    collections: [],
    repository: { owner: "owner", name: "repo", branch: "main" },
  };
  const client = new RappBase({
    baseUrl: "https://example.test/",
    repository: "owner/repo",
    fetch: async () => response(200, registry),
  });
  assert.throws(
    () => client.collection("resources").prepareCreate(
      { summary: "x".repeat(5000) },
      { commandId: "123e4567-e89b-42d3-a456-426614174012" },
    ),
    (error) => error.code === "invalid_data",
  );
  await client.getRegistry();
  assert.doesNotThrow(() => client.collection("resources").prepareCreate(
    { summary: "x".repeat(5000) },
    { commandId: "123e4567-e89b-42d3-a456-426614174012" },
  ));

  const overridden = new RappBase({
    baseUrl: "https://example.test/",
    limits: { string_bytes: 100 },
    fetch: async () => response(200, registry),
  });
  await overridden.getRegistry();
  assert.throws(
    () => overridden.collection("resources").prepareCreate(
      { summary: "x".repeat(101) },
      { commandId: "123e4567-e89b-42d3-a456-426614174013" },
    ),
    (error) => error.code === "invalid_data",
  );
});

test("polling enforces in-flight timeout and distinguishes caller abort", async () => {
  const never = async () => new Promise(() => {});
  const client = new RappBase({
    baseUrl: "https://example.test/",
    repository: "owner/repo",
    fetch: never,
  });
  await assert.rejects(
    () => client.pollReceipt(
      "123e4567-e89b-42d3-a456-426614174020",
      { timeoutMs: 20 },
    ),
    (error) => error instanceof RappBaseError && error.code === "timeout",
  );

  let listener;
  let removed = 0;
  const signal = {
    aborted: false,
    addEventListener(_name, value) { listener = value; },
    removeEventListener(_name, value) {
      if (listener === value) removed += 1;
    },
  };
  const pending = client.pollReceipt(
    "123e4567-e89b-42d3-a456-426614174021",
    { signal, timeoutMs: 1000 },
  );
  signal.aborted = true;
  listener();
  await assert.rejects(
    () => pending,
    (error) => error instanceof RappBaseError && error.code === "aborted",
  );
  assert.equal(removed, 1);
});

test("receipt polling validates complete shape and command identity", async () => {
  const requested = "123e4567-e89b-42d3-a456-426614174030";
  const client = new RappBase({
    baseUrl: "https://example.test/",
    fetch: async () => response(
      200,
      receipt("123e4567-e89b-42d3-a456-426614174031"),
    ),
  });
  await assert.rejects(
    () => client.pollReceipt(requested, { timeoutMs: 100 }),
    (error) => error instanceof RappBaseError && error.code === "protocol",
  );
});

test("record validation checks full shape and requested identity", async () => {
  const client = new RappBase({
    baseUrl: "https://example.test/",
    fetch: async () => response(200, records[1]),
  });
  await assert.rejects(
    () => client.collection("resources").getOne("one"),
    (error) => error instanceof RappBaseError && error.code === "protocol",
  );
  const malformed = { ...records[0] };
  delete malformed.owner_id;
  const bad = new RappBase({
    baseUrl: "https://example.test/",
    fetch: async () => response(200, malformed),
  });
  await assert.rejects(
    () => bad.collection("resources").getOne("one"),
    (error) => error instanceof RappBaseError && error.code === "protocol",
  );
});

test("validates complete tombstones and applied receipt references", async () => {
  const tombstone = {
    schema: "rapp-base-tombstone/1.0",
    collection: "resources",
    created_at: timestamp,
    deleted: true,
    deleted_at: timestamp,
    id: "one",
    owner_id: 7,
    prior_revision: "a".repeat(64),
    revision: "b".repeat(64),
    updated_at: timestamp,
  };
  const recordClient = new RappBase({
    baseUrl: "https://example.test/",
    fetch: async () => response(200, tombstone),
  });
  assert.equal(
    (await recordClient.collection("resources").getOne("one")).deleted,
    true,
  );

  const commandId = "123e4567-e89b-42d3-a456-426614174032";
  const eventHash = "c".repeat(64);
  const applied = receipt(commandId, {
    code: "created",
    event: {
      hash: eventHash,
      path: `00000001-${eventHash.slice(0, 12)}.json`,
      sequence: 1,
    },
    record: {
      collection: "resources",
      deleted: false,
      id: "one",
      revision,
    },
    status: "applied",
  });
  const receiptClient = new RappBase({
    baseUrl: "https://example.test/",
    fetch: async () => response(200, applied),
  });
  assert.equal(
    (await receiptClient.pollReceipt(commandId, { timeoutMs: 100 })).status,
    "applied",
  );
});

test("filters use JSON-semantic equality without relational coercion", async () => {
  const items = [
    { ...records[0], data: { value: { a: 1, b: 2 }, rank: "5" } },
    { ...records[1], data: { value: { b: 2, a: 1 }, rank: 5 } },
  ];
  const client = new RappBase({
    baseUrl: "https://example.test/",
    fetch: async () => response(200, snapshot(items)),
  });
  const equal = await client.collection("resources").getFullList({
    filter: { field: "data.value", op: "eq", value: { b: 2, a: 1 } },
  });
  assert.equal(equal.length, 2);
  const relational = await client.collection("resources").getFullList({
    filter: { field: "data.rank", op: "gte", value: 5 },
  });
  assert.deepEqual(relational.map((item) => item.id), ["two"]);
});

test("string sorting uses deterministic Unicode code-point order", async () => {
  const items = [
    { ...records[0], data: { title: "\u{10000}" } },
    { ...records[1], data: { title: "\uE000" } },
  ];
  const client = new RappBase({
    baseUrl: "https://example.test/",
    fetch: async () => response(200, snapshot(items)),
  });
  const sorted = await client.collection("resources").getFullList({
    sort: { field: "data.title", direction: "asc" },
  });
  assert.deepEqual(sorted.map((item) => item.id), ["two", "one"]);
});
