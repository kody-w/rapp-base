# Sol to Claude Fable Handoff

Updated: 2026-07-21

## Continuation trigger

If the user says **"continue what Sol was doing"**, do not start a new design
pass. Verify the heads below, run the primary validation command, and continue
the first ready task under **Next work**.

## Repositories

| Role | Local worktree | GitHub | Expected `main` |
| --- | --- | --- | --- |
| Hosted reference and canonical framework | `/Users/rappterone/Documents/GitHub/rapp-base` | `kody-w/rapp-base` | tag `sol-handoff-2026-07-21` |
| Clean zero-state template | `/Users/rappterone/Documents/GitHub/rapp-base-template` | `kody-w/rapp-base-template` | `0242efba7d3e987528e450f2f5e74c4a88fd3cbb` |

Both worktrees were clean and synchronized with `origin/main` at handoff.
Both public repositories are on release `v1.2.0`.
The final feature commit before the handoff metadata is
`e28945e88cbe48652108de2f754b59fa5f00db04`.

Public surfaces:

- Reference: <https://kody-w.github.io/rapp-base/>
- Template: <https://kody-w.github.io/rapp-base-template/>
- Framework release: <https://github.com/kody-w/rapp-base/releases/tag/v1.2.0>
- Template release: <https://github.com/kody-w/rapp-base-template/releases/tag/v1.2.0>

## Completed work

The following is implemented, published, and should not be redesigned without
new evidence:

- `rapp-static-api/1.0` read plane and `rapp-base/1.0` profile.
- Generic schema-driven collections and bounded static snapshots.
- Public GitHub Issue create/update/delete with deterministic IDs.
- Optimistic full-SHA revisions, owner policy, tombstones, and durable receipts.
- Hash-chained events and byte-addressed immutable versions.
- Strict parser limits, duplicate-key rejection, safe-number rules, and exact
  Unicode code-point preservation.
- Direct admission from `issues: opened`; Search is recovery only.
- Crash-atomic immutable publication and monotonic Git-history checks.
- Canonical/main, Raw, and Pages full-registry digest canary.
- File-authoritative write gate at `.rapp-base/write-control.json`.
- Guarded pause/cancel/wait/resume tooling.
- Python 3.12-3.14 CI and zero-dependency JavaScript SDK.
- No-op-aware Pages decision/deploy split.
- Required Issue Form publication attestation while preserving raw JSON and
  the committed v1.0 SDK wrapper.
- Guarded zero-state `scripts/bootstrap.py`.
- Clean public template separate from the admitted reference deployment.
- Scale probe and initial 250/1,000-event measurements.

The hosted reference has three intentional smoke-test events: create, update,
and delete. The record is currently a tombstone. The template has zero
requests, receipts, and events.

## Non-negotiable invariants

1. Never delete, rewrite, or reset the reference ledger or indexed versions.
2. Never admit demo commands into `rapp-base-template`; it must remain zero-state.
3. Do not hand-edit `state/`, `api/`, `versions/`, or `registry.json`.
4. Use `manifest.json` plus the deterministic builder for generated changes.
5. Replay-critical schema or seed changes after first admission require a new
   major/migration design; v1 intentionally refuses them.
6. `.rapp-base/write-control.json` is the sole write authority. Missing means
   enabled only for backward compatibility; committed repos contain canonical
   `enabled: true`.
7. Keep action references pinned to full commit SHAs.
8. A no-op processor may run a Pages decision job but must not package or
   deploy Pages.
9. Raw JSON and the legacy v1.0 wrapper remain supported programmatic paths.
10. Public deletion is a tombstone, not erasure. Never claim privacy, auth,
    realtime, files, or low-latency writes.

## Validation

Primary framework:

```bash
cd /Users/rappterone/Documents/GitHub/rapp-base
git fetch origin main
git fetch origin tag sol-handoff-2026-07-21
git merge-base --is-ancestor sol-handoff-2026-07-21 HEAD
git status --short --branch
PYTHON=python3.14 make check
PYTHON=python3.14 make build   # must report changed: 0
python3.14 scripts/check_monotonic.py --base origin/main
```

Template:

```bash
cd /Users/rappterone/Documents/GitHub/rapp-base-template
git fetch origin main
git status --short --branch
PYTHON=python3.14 make check
PYTHON=python3.14 make build   # must report changed: 0
find state/events state/requests state/receipts -name '*.json' -type f
# The find command must print nothing.
```

Hosted boundary check:

```bash
TOKEN="$(gh auth token)"
cd /Users/rappterone/Documents/GitHub/rapp-base
GITHUB_TOKEN="$TOKEN" GITHUB_REPOSITORY="kody-w/rapp-base" \
  python3.14 scripts/check_live.py
GITHUB_TOKEN="$TOKEN" GITHUB_REPOSITORY="kody-w/rapp-base" \
  python3.14 scripts/write_control.py check
```

Repeat with the template worktree and repository name.

## Next work

### 1. Public demo - ready

Create a concise public create -> update -> delete walkthrough without adding
new canonical records. Prefer using the existing live evidence:

- Issues `#1`, `#2`, and `#3`.
- Events `1`, `2`, and `3`.
- Their receipts, tombstone, and three immutable versions.

Recommended output: a small static `demo.html` or animated walkthrough linked
from the README and explorer. It must read existing public JSON and must not
submit new Issues automatically.

After implementation, synchronize deployment-neutral changes into
`rapp-base-template`, adapt deployment URLs, validate both repositories, commit,
push, and cut the next matching release only if behavior changed.

### 2. Adoption launch - ready after demo

Publish a concise announcement and deployment invitation linking:

- Clean template: <https://github.com/kody-w/rapp-base-template>
- Feedback: <https://github.com/kody-w/rapp-base/discussions/4>
- Decision gates: <https://github.com/kody-w/rapp-base/discussions/5>

Success evidence is two external template deployments, first-write timing, and
feedback from users not involved in implementation. Do not infer adoption from
Actions clones.

### 3. Ecosystem registration - blocked

Research was completed at these heads:

- `rapp-static-apis`:
  `77f81ec6f20c28662dfb4b6c8293f88370ef26f2`
- `rapp-map`:
  `fcf1f7dbbbecdeada339e4cfce823b7c2658b7e9`
- `RAPP-Bible`:
  `c88fe94f9fe5d48791b9593fb941cb707fb939bc`

Do not make a three-repository registration change yet:

- `rapp-static-apis` discovers only local subdirectories and has no external
  inventory. Adding RAPP Base requires an explicit external-inventory design;
  hand-editing generated `registry.json` would be wrong.
- `rapp-map` deliberately is not an ecosystem registry. Its old inventory is
  quarantined and its evidence files/node sets are intentionally fixed. Do not
  add RAPP Base there as routine registration.
- `RAPP-Bible` names `scripts/build_repo_pages.py::INVENTORY` as source, but
  the generator would replace the newer family-grouped checked-in index with a
  smaller tier-only index. Reconcile that generator drift before adding:

  ```python
  ("rapp-base", 2, "Hosted RAPP Base reference deployment and canonical framework source"),
  ("rapp-base-template", 2, "Clean zero-state template for deploying RAPP Base static backends"),
  ```

A documentation-only reference in `rapp-static-apis` may be reasonable after
review, but it is not machine registration.

## Evidence-gated future work

Do not implement these until their gate is reached:

- Shard/checkpoint before a generated index exceeds 400 KiB or roughly 750
  similarly shaped events.
- Design migrations only after two real deployments need the same migration.
- Add an external availability witness before making an SLA claim.
- Add moderation/approval profiles after unsuitable content or measurable
  moderation burden appears.
- Publish a typed package after repeated consumer demand.
- Design federation only after two independent deployments need shared
  identity, revocation, or merge semantics.

## Monitoring state

There is no active Copilot scheduled prompt at handoff. GitHub-hosted processor
and operational-canary workflows remain scheduled every six hours in both
repositories. The latest observed processor, canary, CI, and Pages runs were
successful, both write controls were enabled, registry digests matched across
canonical/Raw/Pages, and there were no open command Issues.

## Commit conventions

Use descriptive conventional commits. Keep the framework and template commits
aligned when behavior is shared. Include the required Copilot session trailers
when the current environment requires them. Never force-push or rewrite the
published ledgers.
