# Security model

## Public-data boundary

RAPP Base is only for intentionally public, non-sensitive data. GitHub Issues,
commits, immutable versions, events, receipts, and tombstones are durable
public artifacts. Deleting a record does not remove prior values from Git
history or GitHub. Never submit credentials, tokens, private URLs, personal
data, regulated data, or content you are not allowed to publish.

The required Issue Form checkbox records the submitter's assertion that they
have publication rights and supplied no secrets or private/personal data.
Submitting a raw-JSON command or an exact legacy v1.0 SDK wrapper
programmatically constitutes the same assertion. The legacy wrapper is not an
Issue Form path. None of these paths proves rights, scans for sensitive data,
or establishes that content is suitable; maintainers remain responsible for
moderation.

There is no custom authentication or authorization server. A `public` mutation
means a GitHub-authenticated Issue author, identified by the numeric user ID in
trusted GitHub API metadata. It does not mean anonymous writes.

## Controls

- Command-supplied actor, owner, revision, path, and system fields cannot
  override trusted identity or record metadata.
- Issue database and node IDs, repository identity, actor numeric ID, and
  author association are retained in immutable envelopes/events.
- JSON rejects duplicate keys, non-finite numbers, unknown fields, control
  characters, unsafe numeric magnitudes, traversal-like
  paths, excessive size/depth/nodes, and multiple candidates.
- Invalid admissions retain body/candidate hashes, snapshotted parser
  settings, and a stable error without copying rejected raw text into the
  immutable request. This minimizes duplicate retention but cannot remove the
  original text from the public GitHub Issue.
- Policies, ownership, schema, uniqueness, and optimistic revision are checked
  at append time.
- Organization `MEMBER` association grants no maintainer or owner-recovery
  authority; owner recovery requires the numeric record owner or repository
  `OWNER`.
- Events and records are SHA-256 addressed; events are hash chained.
- Generated immutable versions are indexed and cannot silently mutate or
  disappear. Git history is the external anchor against a coordinated rewrite
  of both a version and its index metadata.
- Immutable publication stages and fsyncs complete same-directory bytes before
  atomic no-replace publication. CI/processing compare prior Git objects and
  reject non-monotonic immutable history.
- Workflows use the ephemeral `GITHUB_TOKEN`, least job permissions, global
  serialization, pinned action commits, and no `pull_request_target`.
- Issue text reaches Python only through GitHub REST JSON or the trusted
  `GITHUB_EVENT_PATH`; it is never interpolated into a shell command or
  workflow expression.
- Repository checks and the Pages allowlist reject symlinks, including broken
  links and links that escape the repository.
- The only network adapter calls GitHub's fixed REST origin. User URL fields
  are syntax validated but never fetched.
- Browser rendering uses `textContent` and created DOM nodes for public data.
  Tokens passed to optional SDK submission are used once and never persisted.
- Receipt comments count as delivered only when their complete expected body
  is authored by the configured trusted Actions bot.

These controls do not protect readers from data that an authorized public
author intentionally submits. Repository maintainers remain responsible for
moderation and legal takedowns; Git history rewriting may be required for an
emergency secret exposure and is outside the normal deletion protocol.

`healthy` reports only verified repository history/projection integrity. It is
not an availability or freshness attestation for GitHub REST, Actions, Pages,
DNS, or raw-content delivery.

## Emergency response

To stop mutation processing during an incident while preserving public reads,
use the guarded operator command:

```sh
GITHUB_TOKEN="$(gh auth token)" \
  GITHUB_REPOSITORY="kody-w/rapp-base" \
  python3 scripts/write_control.py pause \
  --confirm-repository kody-w/rapp-base
```

This atomically commits `.rapp-base/write-control.json` with
`enabled: false` to `main` using the current blob SHA, cancels all queued or
in-progress `process.yml` runs, and waits until none remain. It reports `pause
complete` only after the committed document reads false and the active-run
list is empty. Contents/API uncertainty fails closed and visibly. The token
needs Contents write plus Actions read/cancel access.

This does not make submitted data private, disable the Issue form, or close
queued commands; those Issues remain public and open. Moderate exposed content
through GitHub, rotate any credential immediately, and follow the
repository-history rewrite procedure when removal from normal projections is
insufficient.

After investigating the processor, published generations, and Pages state, run
the `Operational canary` manually. Resume with:

```sh
GITHUB_TOKEN="$(gh auth token)" \
  GITHUB_REPOSITORY="kody-w/rapp-base" \
  python3 scripts/write_control.py resume \
  --confirm-repository kody-w/rapp-base
```

Resume commits and confirms `enabled: true`; it never deletes control history.
The committed document on `main` is the sole write authority. The processor
checks it before reconciliation and immediately before push. A paused workflow
may start, but its first file gate exits before reconciliation. A missing
document enables writes only for upgrade compatibility; duplicates, unknown
fields, malformed/oversized content, or uncertain API responses block writes.

The canary measures the hosted boundary but is not an SLA or a provider-
independent external monitor. A GitHub-wide failure can affect both RAPP Base
and the GitHub Actions canary.

## Reporting a vulnerability

Do not place exploit details or secrets in a public Issue or Discussion. Use
[GitHub private vulnerability reporting](https://github.com/kody-w/rapp-base/security/advisories/new)
and include the affected commit, minimal reproduction, impact, and proposed
mitigation. If GitHub reports that private reporting is unavailable, use a
private contact method listed on the
[maintainer's GitHub profile](https://github.com/kody-w). Send only a
non-sensitive summary until a secure channel is established; do not paste
credentials, private data, or exploit details into any public fallback.

For ordinary malformed public commands or stale revisions, open a new command
Issue; edits to an admitted Issue are intentionally ignored.
