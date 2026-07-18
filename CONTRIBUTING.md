# Contributing

RAPP Base keeps its trusted implementation intentionally small.

1. Discuss behavior changes before investing in a large patch.
2. Use Python 3.12 standard library only for the engine/build/check/tests.
3. Keep the browser and ESM SDK at zero runtime dependencies.
4. Do not add package/dependency manifests unless a concrete toolchain need
   cannot be met by existing runtimes.
5. Preserve the separation between pure parsing/reducer logic and the GitHub
   REST adapter.
6. Never hand-edit canonical request/receipt/event files or generated API
   output to simulate a mutation.
7. Do not weaken strict parsing, identity, policy, optimistic concurrency,
   append-only versions, or deterministic/no-wall-clock behavior.
8. Pin every workflow action to a reviewed 40-character commit SHA.

Run the complete local gate:

```sh
make build
make check
```

The first command generates projections. The second is read-only: it rejects
stale bytes, runs Python unit/end-to-end tests and `node --test`, prepares the
Pages artifact, and verifies repository/workflow invariants. The worktree must
finish without caches or scratch directories.

Changes to public behavior should update `SPEC.md`, discovery metadata, schemas,
SDK behavior, and tests together. New fields must fit the constrained manifest
type system; arbitrary executable validators and hooks are out of scope.

By contributing, you agree that your contribution is licensed under the MIT
License.
