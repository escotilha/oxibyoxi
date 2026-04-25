# Release notes

oxi follows [PEP 440](https://peps.python.org/pep-0440/) versioning: `0.1.0a*` → `0.1.0b*` → `0.1.0` stable.

## Beta

- [v0.1.0b1](v0.1.0b1.md) — first beta cut

## Alpha

- [v0.1.0a6](v0.1.0a6.md)
- [v0.1.0a5](v0.1.0a5.md)
- [v0.1.0a4](v0.1.0a4.md)
- [v0.1.0a3](v0.1.0a3.md)
- [v0.1.0a2](v0.1.0a2.md)
- [v0.1.0a1](v0.1.0a1.md)
- [v0.0.0](v0.0.0.md) — pre-alpha snapshot

## Versioning policy

See [semver-contract.md](../semver-contract.md) for the full versioning contract. TL;DR: alpha releases (`a*`) may break adapters; beta releases (`b*`) freeze the adapter protocol within a minor; stable (`0.1.0`) freezes the public surface for the lifetime of the minor.

## Authoring rule

One file per release: `v0.1.0.md`, `v0.2.0.md`, etc. Each file has exactly one headline change per [anti-patterns §1](../anti-patterns.md#1-one-release-ships-one-headline-change).
