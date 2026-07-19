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

Rejected admissions retain a SHA-256 of the Issue body, a candidate-command
hash when a candidate was extractable, snapshotted parser limits/profile, and
a stable error. They do **not** copy raw rejected body/candidate text into the
immutable request. The original public Issue can still retain that text.
Valid admissions retain the normalized command and its exact submitted-command
hash; the three original v1 requests with legacy command-text snapshots remain
replay compatible.

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

The live `rapp-base` repository has admitted history and is **not** the clean
template. Start from
[`kody-w/rapp-base-template`](https://github.com/kody-w/rapp-base-template),
whose head sequence is `0` and whose request, receipt, and event directories
contain no JSON files.

1. Select **Use this template**, clone the new repository, then bind every
   deployment URL and fixture identity in one guarded command:

   ```sh
   python3 scripts/bootstrap.py \
     --root . \
     --owner example-owner \
     --repo example-data
   ```

   The command refuses admitted state, symlinks, unsafe GitHub names, and a
   dirty or staged checkout. It never rewrites `.git`, removes only generated
   `api/`, `versions/`, and `registry.json`, re-anchors the zero-state head,
   and deterministically rebuilds the deployment.
2. Review `manifest.json`, then customize collections, fields, seeds,
   policies, limits, and explicit semantic `generated_at` only while the
   deployment still has zero requests, receipts, and events. Re-run the
   normal builder after semantic customization.
3. Create the public GitHub repository, enable Issues and Actions, and permit
   the processor's scoped `contents: write` and `issues: write`
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
permission. An `issues: opened` run admits its trusted event Issue directly
and merges/deduplicates it with the recovery scan. Manual recovery remains
available; the scheduled recovery scan runs every six hours and queries up to
100 oldest open matching Issues. Successful delivery closes them. A
user-closed Issue that was never admitted is skipped. GitHub Search, REST,
Actions, and secondary rate limits still apply.

## Replay and recovery

`state/head.json` and every event anchor a deterministic `genesis_sha256` over
collection names, field schemas, and seeds. A build may re-anchor template
customization only while there are zero events, requests, and receipts. The
first admission, including a rejection, locks genesis. A later replay-critical
schema or seed change fails with `migration_required`. RAPP Base v1 has no
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

Admission order records first durable observation. Existing admission
sequences never move; creation instant and immutable Issue database ID order
only newly observed Issues within one reconciliation batch. CI and the
processor compare prior Git objects with the candidate tree and reject a
decreased head, changed genesis, or mutation/removal of prior immutable
state/version-index entries.

## Operations and scaling

`healthy: true` in `status.json` and the registry means the checked repository
history and projections are internally consistent. It does not measure GitHub
API, Actions, raw-CDN, or Pages availability; those are explicitly reported as
`not_measured`.

Collection metadata reports active, tombstone, and lifetime record counts plus
remaining active slots. `records_per_collection` limits active records, so a
delete frees a create slot. Event and request limits are lifetime ledger
bounds; status reports their utilization and remaining capacity.

Run an isolated stdlib growth probe (it cleans `.scale-work` and never writes
the real state tree):

```sh
python3.14 scripts/scale_probe.py \
  --creates 100 --updates 200 --deletes 25 --rejections 25
python3.14 scripts/scale_probe.py \
  --creates 400 --updates 800 --deletes 100 --rejections 100
```

For this template, investigate before growth reaches any of these operational
warning thresholds: probe time above 120 seconds, estimated Pages artifact
above 100 MiB, request/event utilization above 80%, or any collection with
less than 20% active headroom. These are conservative operator thresholds, not
protocol limits; configured manifest limits and GitHub quotas remain
authoritative. Compare probe JSON over time, especially largest files and
directories, on representative hardware.

See [SPEC.md](SPEC.md), [SECURITY.md](SECURITY.md), and
[CONTRIBUTING.md](CONTRIBUTING.md).
