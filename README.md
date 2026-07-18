# RAPP Base

RAPP Base is a public GitHub-Issue CRUD profile over
`rapp-static-api/1.0`. It publishes deterministic, CORS-readable JSON and
accepts one strict create, update, or delete command per public GitHub Issue.
It is not a private database, realtime server, or low-latency write service.

## Use this RAPP Base

- [Pages explorer](https://kody-w.github.io/rapp-base/)
- [Raw registry](https://raw.githubusercontent.com/kody-w/rapp-base/main/registry.json)
- [Resources snapshot](https://raw.githubusercontent.com/kody-w/rapp-base/main/api/v1/collections/resources/records.json)
- [Versions index](https://raw.githubusercontent.com/kody-w/rapp-base/main/versions/index.json)
- [Open a command Issue](https://github.com/kody-w/rapp-base/issues/new?template=rapp-base-command.yml)
- [Published receipts](https://raw.githubusercontent.com/kody-w/rapp-base/main/api/v1/receipts/index.json)

Read with the zero-dependency ESM SDK:

```js
import { RappBase } from "https://kody-w.github.io/rapp-base/sdk/rapp-base.js";

const db = new RappBase({
  baseUrl: "https://raw.githubusercontent.com/kody-w/rapp-base/main/",
  repository: "kody-w/rapp-base",
});

await db.getRegistry(); // loads this deployment's validation limits
const page = await db.collection("resources").getList(1, 20, {
  filter: { field: "data.topics", op: "contains", value: "python" },
  sort: { field: "data.rating", direction: "desc" },
});
```

Collection snapshots have familiar
`{page, perPage, totalItems, totalPages, items}` keys, but each is one bounded,
complete static page rather than cursor pagination.

## Create, update, and delete

Every command Issue title must start exactly with `[RAPP Base]`. Its body must
be either one command object or the exact Issue Form `### Command` wrapper.
Labels are optional taxonomy and are never routing authority.

Create (the processor derives the record ID):

```json
{
  "schema": "rapp-base-command/1.0",
  "command_id": "123e4567-e89b-42d3-a456-426614174000",
  "operation": "create",
  "collection": "resources",
  "data": {
    "title": "An open resource",
    "url": "https://example.com/resource",
    "kind": "article",
    "summary": "A useful public resource.",
    "topics": ["example"],
    "free": true,
    "rating": 4
  }
}
```

Update (partial merge using the current full revision):

```json
{
  "schema": "rapp-base-command/1.0",
  "command_id": "123e4567-e89b-42d3-a456-426614174001",
  "operation": "update",
  "collection": "resources",
  "record_id": "r_0123456789abcdef01234567",
  "if_revision": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "data": {
    "rating": 5
  }
}
```

Delete (publishes a tombstone):

```json
{
  "schema": "rapp-base-command/1.0",
  "command_id": "123e4567-e89b-42d3-a456-426614174002",
  "operation": "delete",
  "collection": "resources",
  "record_id": "r_0123456789abcdef01234567",
  "if_revision": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

Replace the example record ID and revision with values from the current
record. Issue creation is only admission, not commit acknowledgement. Poll the
command receipt; raw CDN URLs can briefly cache older branch content.

The SDK prepares commands and safe Issue URLs:

```js
const draft = db.collection("resources").prepareUpdate(
  record.id,
  record.revision,
  { rating: 5 },
);

if (draft.requiresCopy) {
  // Open draft.issueUrl and paste draft.json into the Command field.
}
```

Very long commands deliberately receive a template-only URL to avoid a
guaranteed HTTP 414. The optional submit adapter or one-call token path never
persists a token, and REST submission does not request label authority.

## Public and permanent

Issues, immutable requests, receipts, events, record versions, tombstones, and
Git commits are public history. Never submit secrets, personal/private data,
private URLs, regulated data, files, or content you cannot publish. Normal
deletion does not erase Git or Issue history.

Live records carry a semantic `revision`. Immutable version filenames use
`sha8`, RAPP's historical name for the first **12** characters of SHA-256 over
the exact stored bytes. `versions/index.json` retains both `content_sha256`
and `semantic_sha256`.

## Policies and identity

GitHub's numeric Issue-author ID is record identity:

- `public`: any GitHub-authenticated Issue author;
- `collaborator`: repository `OWNER` or GitHub's `COLLABORATOR` association;
- `maintainer`: repository `OWNER` only;
- `owner`: matching record owner ID, with repository `OWNER` recovery;
- `disabled`: nobody.

`MEMBER` means organization membership, not repository write authority, and
has no privileged recovery. GitHub's `author_association` is a coarse signal;
deployments needing exact repository permissions must not strengthen these
static policies without querying a stronger trusted permission source. The
demo uses only `public` create and `owner` update/delete.

## Template/operator setup

1. Create a public repository from the template or fork and enable Issues and
   Actions.
2. Before processing any event, edit `manifest.json` with the real owner,
   repository, collections, fields, seeds, policies, limits, and explicit
   semantic `generated_at`.
3. Permit the processor's scoped `contents: write` and `issues: write`
   `GITHUB_TOKEN` permissions and let it fast-forward `main`; no PAT is used.
4. Select **GitHub Actions** as the Pages source if the explorer is wanted.
5. Generate, then run the read-only validation contract:

   ```sh
   PYTHON=python3.14 make build
   PYTHON=python3.14 make check
   ```

6. Commit the complete initial tree, push `main`, and manually run
   **Process RAPP Base requests** once.

The fixed `rapp-base-request` Issue Form label is optional. Routing uses the
trusted title plus strict body parser, so SDK users do not need label
permission. Scheduled/manual scans query up to 100 oldest open matching Issues
per run; successful delivery closes them. A user-closed Issue that was never
admitted is skipped. GitHub Search, REST, Actions, and secondary rate limits
still apply, so high-volume deployments must lower cadence or use another
backend.

## Replay and recovery

`state/head.json` and every event anchor a deterministic `genesis_sha256` over
collection names, field schemas, and seeds. Before the first event, a build
re-anchors template customization. After any event, a replay-critical schema
or seed change fails with a migration/new-major error. RAPP Base v1 has no
schema migration mechanism: start a new API major/repository or implement an
explicit future migration. Policy, description, and limit changes are allowed
only when immutable admitted history still derives exactly.

The builder replays every admitted request in admission order and derives the
exact expected event chain and receipt documents. A valid event written before
a crash is authoritative; write mode repairs a lagging valid head, while
`--check` reports stale state without mutation. An unindexed immutable version
is adopted only when its path and bytes exactly match the current deterministic
build.

Indexed versions remain append-only. Git history is the external anchor against
a malicious coordinated rewrite of a version file and its index entry.
Receipt comments are accepted as delivered only when the exact expected body
was authored by the configured trusted Actions bot. Delivery failures are
isolated and retried without rolling back already-pushed verified state.

See [SPEC.md](SPEC.md), [SECURITY.md](SECURITY.md), and
[CONTRIBUTING.md](CONTRIBUTING.md).
