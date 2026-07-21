# Claude Fable Continuation

When the user says **"continue what Sol was doing"**, immediately read
[`HANDOFF.md`](HANDOFF.md), verify the recorded repository heads, and continue
the first ready task under **Next work**.

Do not repeat completed architecture, hardening, release, or monitoring work.
Do not reset the live ledger. Do not use the stateful reference repository as
a template. Follow the invariants and validation commands in the handoff.

The primary worktree is this repository:

```text
/Users/rappterone/Documents/GitHub/rapp-base
```

The synchronized clean template is:

```text
/Users/rappterone/Documents/GitHub/rapp-base-template
```
