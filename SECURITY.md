# Security model

## Public-data boundary

RAPP Base is only for intentionally public, non-sensitive data. GitHub Issues,
commits, immutable versions, events, receipts, and tombstones are durable
public artifacts. Deleting a record does not remove prior values from Git
history or GitHub. Never submit credentials, tokens, private URLs, personal
data, regulated data, or content you are not allowed to publish.

There is no custom authentication or authorization server. A `public` mutation
means a GitHub-authenticated Issue author, identified by the numeric user ID in
trusted GitHub API metadata. It does not mean anonymous writes.

## Controls

- Command-supplied actor, owner, revision, path, and system fields cannot
  override trusted identity or record metadata.
- Issue database and node IDs, repository identity, actor numeric ID, and
  author association are retained in immutable envelopes/events.
- JSON rejects duplicate keys, non-finite numbers, unknown fields, control
  characters, traversal-like paths, excessive size/depth/nodes, and multiple
  candidates.
- Policies, ownership, schema, uniqueness, and optimistic revision are checked
  at append time.
- Organization `MEMBER` association grants no maintainer or owner-recovery
  authority; owner recovery requires the numeric record owner or repository
  `OWNER`.
- Events and records are SHA-256 addressed; events are hash chained.
- Generated immutable versions are indexed and cannot silently mutate or
  disappear. Git history is the external anchor against a coordinated rewrite
  of both a version and its index metadata.
- Workflows use the ephemeral `GITHUB_TOKEN`, least job permissions, global
  serialization, pinned action commits, and no `pull_request_target`.
- Issue text is written only to fixture JSON handled by Python; it is never
  interpolated into a shell command or workflow expression.
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
