# Public identity-registry contract

This is the target permanent-identity contract, not a currently deployed Oběhy component. The
registry will be implemented in a separate repository after one PID static-overlay build and one
PID realtime entity work end to end against the finalized serving database.

Until then JrUtil emits `identity_contract = "provisional-v0"` with opaque `v0:` IDs. Those IDs are
not imported into the registry and carry no cross-build stability promise. Registry launch creates
the single declared public-ID break and changes the compiler to `identity_contract = "registry-v1"`.

The eventual registry is a standalone FastAPI service with its own PostgreSQL database. Public
reads are anonymous; mutations require OIDC roles. JrUtil and Oběhy use its HTTP API and immutable
snapshots, never its database tables.

## IDs and domains

```text
S000000001                   surface stop place
P000000001                   surface boarding point/post
rail:CZ:<SR70>               railway primary location
rail:CZ:<SR70>:<subsidiary>  railway subsidiary/platform
C000000001                   operator
R000000001                   route
T000000001                   scheduled trip
V000000001                   vehicle
A000000001                   persistent alert
```

Allocated sequences are independent and non-recycling. Country is ISO 3166-1 alpha-2. SR70 and
subsidiary components use their catalog-normalized strings so leading zeroes are preserved.
Surface and railway locations never merge; an intermodal relationship is a separate association.

The registry stores canonical type/domain/name/coordinates, parent relationships, bindings,
aliases, redirects, tombstones, revisions and evidence. A reviewed railway-coordinate override
retains the original SR70 value, reason, evidence, author and timestamp.

## Reconciliation

JrUtil submits an idempotent proposal batch against an immutable base snapshot. Each proposal
contains source/snapshot identity, entity kind/domain, source object ID, validity, normalized
identity facts and ordered candidate evidence. The registry returns accepted mappings, new IDs,
quarantines and the resulting snapshot digest in one transaction.

For JDF surface stops, automatic continuity requires one unique normalized `(full stop name,
actual JDF district code, country)` candidate. Generated JDF stop numbers are provenance only.
Duplicates or conflicts are quarantined. Railway locations resolve deterministically from their
country-scoped SR70 identity.

Merges create redirects and never erase history. Retirement creates a tombstone. Manual decisions
and coordinate corrections are revisions and therefore require a new immutable snapshot.

## Public interfaces

- OpenAPI REST entity lookup, search, bindings, redirects and revision history.
- OIDC-protected batch proposal, quarantine review, redirect and coordinate-correction endpoints.
- Immutable CSV and Parquet snapshots with canonical JSON manifests and SHA-256 hashes.
- `viewer`, `operator`, `editor` and `administrator` roles.
