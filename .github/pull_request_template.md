## Reviewer checklist

The Conformance gate cannot see these conventions — confirm each by reading the diff.

- [ ] **Fail closed** — admission, compatibility matching, and sandbox probing refuse rather than guess.
- [ ] **Strictly typed classes** — full attribute annotations, explicit `__init__ -> None` returns, `typing.Self` for factory or builder methods.
- [ ] **Protocol-based interfaces** — no runtime `NotImplementedError` stubs standing in for a contract (`abc.ABC` is already lint-banned).
- [ ] **Small and focused** — functions and modules stay small; private helpers are `_`-prefixed.

## Do not modify

- [ ] Sandbox isolation / admission / certification-gating code changed only under explicit review, and the `docs/traceability.md` rows were written before the code that implements them.
