# RAPP Base 1.0 specification

## 1. Scope and envelope

RAPP Base is a deterministic public CRUD profile over
`rapp-static-api/1.0`. Protocol documents use a `schema` discriminator.
`$schema` appears only in JSON Schema documents as the Draft 2020-12 dialect
keyword.

The root registry keeps:

```json
{
  "schema": "rapp-static-api/1.0",
  "profile": "rapp-base/1.0"
}
```

It also exposes base `generated`, `summary`, and `entries` members plus
profile-specific collections, capabilities, URLs, generation hash, and an
absolute `versions_url`. Readers consume bounded static JSON. There is no live
MCP, A2A, plugin, websocket, or application server.

## 2. Trust and routing

The processor trusts repository and Issue database/node IDs, Issue number,
title, timestamps, numeric author ID, association, state, labels, and body only
as returned by GitHub REST. Identity never comes from a login or command.

An official request is an open, non-PR Issue whose trusted title starts
exactly `[RAPP Base]`. Labels are optional taxonomy, not authority. The
scheduled/manual reconciler searches all such open Issues; every Issue-open
event starts the processor too. A manually closed Issue not yet admitted may
be skipped. Terminal state is in Git, so successfully delivered closed Issues
need not remain in the queue.

The body is either one object or exactly:

    ### Command

    ```json
    { one object }
    ```

No extra Markdown, fence, JSON candidate, or trailing text is accepted.
Malformed official requests still receive immutable request envelopes and
terminal rejections.

## 3. Commands

Commands use `schema: "rapp-base-command/1.0"` and require a canonical,
non-zero lowercase UUID `command_id`, operation, and collection.

- create requires `data`, and forbids `record_id`/`if_revision`;
- update requires `record_id`, full SHA-256 `if_revision`, and partial `data`;
- delete requires `record_id` and full SHA-256 `if_revision`, and forbids data.

Unknown keys, duplicate keys, non-finite/unsafe numbers, invalid Unicode,
control characters, path traversal, excessive bytes/depth/nodes/keys/items,
and reserved system fields fail closed. URL fields are syntax-checked absolute
HTTPS URLs and are never fetched.

The first admitted command bytes reserve a command ID. Exact-byte reuse is a
terminal no-op. Different bytes with the same ID are a terminal conflict.
Issue edits never retry admission.

## 4. Manifest, genesis, and migration

`manifest.json` uses `schema: "rapp-base-manifest/1.0"`. It defines repository,
collections, fields, seeds, policies, and bounds. Supported field types are
`string`, `number`, `integer`, `boolean`, and `string[]`, with the constraints
validated by `schemas/manifest.schema.json` and the engine.

`genesis_sha256` is canonical SHA-256 over replay-critical collection names,
field schemas, and seeds. Descriptions, policies, and limits are excluded.
`state/head.json` and every event carry this anchor.

Before the first event, write-mode build deterministically re-anchors a
customized template. Once any event exists, an incompatible field-schema or
seed change fails explicitly. RAPP Base v1 defines no schema migration:
operators must start a new API major/repository or add an explicit future
migration mechanism. Policy, description, and limit changes are valid only
when snapshotted admissions and the complete immutable history still derive
the same outcomes.

## 5. Admission durability

Each immutable request stores trusted actor/source identity, title, admitted
time, exact command text/hash when extractable, parse result, and a snapshot of
the parser profile, configured limits, and applicable policy.

Request envelope loading uses separate compiled structural ceilings. It does
not parse raw command text under current manifest limits. The command is then
reproduced under its snapshotted limits, all bounded by compiled hard maxima.
Consequently over-limit strings, key-heavy/deep values, and malformed bodies
remain reloadable terminal rejections even if operator limits later change.

## 6. Authorization

| Policy | Authorized actor |
| --- | --- |
| `public` | Any GitHub-authenticated Issue author with numeric ID |
| `owner` | Matching record-owner ID, or repository `OWNER` recovery |
| `collaborator` | `OWNER` or `COLLABORATOR` association |
| `maintainer` | `OWNER` association |
| `disabled` | Nobody |

`MEMBER` is organization membership and grants no repository-write,
maintainer, or owner-recovery authority. `author_association` is coarse and can
lag repository permission changes. Deployments requiring exact collaborator
permissions need a stronger trusted permission check. Demo collection
policies remain `public` create and `owner` update/delete.

## 7. Records and time

Create IDs are `r_` plus 24 hex characters from canonical SHA-256 over
repository ID, Issue ID/node ID, command UUID, and collection. A live record's
semantic `revision` is SHA-256 over canonical record semantics without the
revision member.

Updates merge a patch into current data, retain identity/creation time, enforce
schema/uniqueness, and reject stale or value-equivalent changes. Deletes
produce tombstones and preserve `prior_revision`.

RFC 3339 timestamps are parsed as instants, never compared lexically.
Admission order is `(created instant, immutable Issue database ID)`.
Updated/deleted/generated maxima use instant order and deterministic Issue-ID
ties.

## 8. Complete-history verification

Canonical state consists of:

- `state/requests/issue-<database-id>.json`;
- `state/receipts/issue-<database-id>.json`;
- `state/events/<sequence>-<event-hash12>.json`;
- `state/head.json`.

Events are contiguous and hash chained from 64 zeroes. Build independently
reduces every request from anchored genesis in admission order. It re-derives
parse rejection, identical replay, ID conflict, policy/schema/unique/stale
rejection, or exact applied event and resulting record. The expected event
chain and every complete receipt document must byte-semantically equal
canonical state. Deleting an event and forging a self-consistent rejection
therefore fails.

## 9. Crash recovery and append-only versions

A valid contiguous event tail is authoritative. A head that validly references
an earlier point is stale: write mode repairs it, while `--check` reports it
without mutation. Ahead or forked heads are never accepted.

`versions/index.json` records `content_sha256` and `semantic_sha256`.
`sha8` is RAPP's historical field name for the first **12** characters of the
SHA-256 of exact stored bytes. Record, request, and collection version
filenames all use that content prefix; record semantic revisions remain
separate.

Indexed versions cannot mutate or disappear. An unindexed file is adopted only
when its exact path and bytes are in the current deterministic desired set;
otherwise build fails. Each API major reads only its own build index when
pruning mutable projections, so a future `api/v2` builder cannot delete
`api/v1`. Git history is the external anchor against a coordinated malicious
rewrite of version bytes and index metadata.

## 10. Build and delivery

`make build` is the only generation step. `make check` starts with
`scripts/build.py --check`, runs Python and Node tests, prepares/checks the
Pages artifact, and runs repository invariants without changing generated
state.

The processor recomputes from refreshed `origin/main`, builds, checks, commits,
and fast-forwards state. Receipt delivery is a later, non-blocking step.
Before commenting it verifies the exact receipt is reachable on remote main.
Only the exact expected comment authored by `github-actions[bot]` (or an
explicit trusted bot login) counts as delivered. Failures are isolated per
Issue; close and label cleanup share one patch, failures are aggregated, and a
later run retries without undoing the verified push or blocking Pages.

The default scan admits at most 100 oldest open matching Issues per run.
GitHub Search (including its 1,000-result ceiling), REST rate/secondary limits,
Actions minutes, repository size, and raw-CDN caching remain independent
operational quotas.

## 11. Explicit non-goals

RAPP Base does not provide privacy, custom authentication, hard deletion,
files, arbitrary hooks/code, outbound retrieval, SQL/joins, live MCP/A2A,
websockets, realtime guarantees, or PocketBase/Firebase wire compatibility.
