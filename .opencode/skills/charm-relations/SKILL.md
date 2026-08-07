---
name: charm-relations
description: >-
  Use when adding, changing, or reviewing a relation endpoint in charmcraft.yaml
  — choosing an interface, deciding which library owns it, optionality, limits,
  databag schemas, and failure modes. Also covers which charm libraries are
  recommended versus deprecated in 2026. Load before writing provides/requires.
metadata:
  verified: "2026-08-06"
---

# Relations

## Decide before you add

Work through this checklist and write the answers into the PR description. If you
cannot answer one, the relation is not ready.

```markdown
# Interface: <interface-name>

## Role
provides | requires | peers — is this charm the producer or the consumer?

## Existing library
Which published library already implements this? Check the official interface
library index before writing anything. If none exists, why does this interface
need to exist, and which repo owns the canonical implementation?

## Optionality
Can the charm reach ActiveStatus without this relation? If yes -> optional: true.
If no -> write the exact BlockedStatus message that tells the operator what to do.

## Limit
limit: 1 or unbounded? The default is unbounded. Be explicit.

## Databag schema
Pydantic model. App-level or unit-level? Versioned? Additions must stay
backwards-compatible within a major version.

## Failure modes
What happens on relation-broken? On relation-changed mid-rotation? Cross-model?
Across a charm upgrade? Does _reconcile cope with this relation appearing and
disappearing on every single event?
```

Default answer to optionality for this charm: **`optional: true`**. The Pi-hole
charm must reach `ActiveStatus` with zero relations. Anything else needs an
argument.

## `optional` and `limit` are not the same kind of key

This distinction matters and is easy to get wrong.

**`optional` is not enforced by Juju.** The reference says so verbatim: *"Not
enforced by Juju, but used by other tools and should always be included."* The
field is parsed and persisted (`charm.Relation.Optional` in the Go source) but
appears in no decision path. It is documentation for Charmhub, `charmcraft`,
linters, and humans.

So `optional: true` does **nothing** operationally. The guarantee that the charm
comes up clean with zero relations lives entirely in `_reconcile` and
`collect_unit_status`. The YAML states the intent; the code has to keep it. Do not
treat a correct `charmcraft.yaml` as evidence.

Note the inverted default: `optional` defaults to `false`, meaning "required". The
reference's own best practice is *"Include the `optional` key in all endpoint
definitions, rather than relying on the default value"* — which is why it is a
non-negotiable here.

**`limit` *is* enforced by Juju, on all three roles.** `AddRelation` calls
`enforceMaxRelationLimit` and returns `QuotaLimitExceeded`. The default is `0`,
meaning unbounded.

More importantly, it is **not safely reversible**. There is a
`preUpgradeRelationLimitCheck` in Juju: if you publish a revision that adds
`limit: 1` to an endpoint where a user already has two relations, **you break their
`juju refresh`**. `optional` is free to add or change; `limit` is a compatibility
commitment. Decide it once, at the start.

`limit` is valid in `provides`, `requires`, and `peers` — not only `requires`.

## `scope` and the implicit `juju-info` endpoint

`scope` is `global` (default) or `container`. `scope: container` goes on the
**subordinate** charm's `requires` side. A subordinate is invalid without at least
one `requires` endpoint with container scope — Juju validates this.

Our charm is the principal, so it declares no scope. But be aware that **every
application implicitly provides a `juju-info` endpoint** (role `provides`,
interface `juju-info`, scope `global`) that cannot be declared or removed, and does
not appear in `juju info`. It exists so subordinates can attach to a principal that
exposes nothing else suitable.

The resolution rule bites: *"If the subordinate also has explicit endpoints whose
interfaces match endpoints on the principal, those explicit endpoints take
precedence over the implicit `juju-info` one."* So `juju integrate pihole
opentelemetry-collector` with no endpoint names may resolve to something other than
`cos-agent`. **In integration tests and any deployment tooling, always name the endpoints
explicitly.**


## Library ecosystem status (2026)

Charmhub-hosted libraries — the single-file modules under `lib/charms/.../vN/`
fetched with `charmcraft fetch-lib` — are being retired
([deprecation explanation](https://documentation.ubuntu.com/charmlibs/explanation/charmhub-libraries-deprecation/)):

> Charmhub-hosted charm libraries [...] are being phased out in favour of standard
> Python packages distributed on PyPI.

Official timeline: charmcraft emits deprecation warnings (current state) → Charmhub
disables uploading **new** libraries (26.10 cycle) → Charmhub disables updates to
existing ones.

**Consequences for this repo:**

- Do **not** create `lib/charms/pihole/v0/*.py` for anything this repo owns.
  There is no `LIBPATCH`/`LIBAPI` discipline to maintain because there are no
  owned Charmhub libraries.
- Consuming an existing Charmhub library is still fine where no PyPI replacement
  exists. `charm-libs:` in `charmcraft.yaml` is the mechanism, and it is *only*
  for those.
- Everything from PyPI goes in `pyproject.toml`.

Authoritative indexes, worth re-checking rather than trusting memory:

- [general libraries](https://documentation.ubuntu.com/charmlibs/reference/general-libs/)
- [interface libraries](https://documentation.ubuntu.com/charmlibs/reference/interface-libs/)

Badges: ✅ recommended, 🚫 deprecated, no badge = neither.

## What this charm plausibly needs

| Interface | Role | Library | Status | Notes |
|---|---|---|---|---|
| `cos_agent` | provides | `charms.grafana_agent.v0.cos_agent` (Charmhub) | no badge | The only Charmhub-hosted library this charm needs. See `charm-cos-integration`. |
| `dns` / DNS-as-a-service | provides | none canonical | — | If exposing Pi-hole as a resolver to other charms, check `charm-relation-interfaces` first. Do not invent an interface without checking. |
| `tls-certificates` | requires | `tls_certificates_interface.v4` | ✅ | Only if serving the admin UI over HTTPS. FTL reads `webserver.tls.cert`. |
| peer | peers | none | — | Needed if multiple units ever have to agree on anything (leader-elected password, coordinated gravity refresh). |

`charmlibs.interfaces.otlp` exists (`charmlibs-interfaces-otlp`, 0.5.0, pre-1.0)
but solves a different problem: it communicates OTLP endpoints for workloads that
*already* speak OTLP. It does not replace `cos_agent`.

There is **no** PyPI package for `cos_agent` — `charmlibs-interfaces-cos-agent`,
`charmlibs-cos-agent`, and `cos-agent` all 404. **NOT VERIFIED** whether Canonical
plans to migrate it; re-check the interface library index before assuming.

## Declaring endpoints

```yaml
provides:
  cos-agent:
    interface: cos_agent
    limit: 1
    optional: true

requires:
  certificates:
    interface: tls-certificates
    limit: 1
    optional: true

peers:
  pihole-peers:
    interface: pihole_peers
```

## Databags

Use pydantic for anything crossing a relation, in both directions. Never write raw
strings into a databag and never `json.loads` a databag without a model.

**`ops` has native support for this since 2.23 — prefer it over hand-rolling.**
`Relation.load(cls, src)` and `Relation.save(obj, dst)` take a pydantic model,
honour `Field(alias=...)`, and default to `json.loads`/`json.dumps` with
overridable `decoder`/`encoder`:

```python
class PiholeProviderData(pydantic.BaseModel):
    """Data this charm publishes on the dns relation."""

    address: str
    port: int = 53


# write
relation.save(PiholeProviderData(address=addr), relation.data[self.app])

# read
data = relation.load(PiholeProviderData, relation.data[remote_app])
```

Only write a manual `dump`/`load` pair if a library forces a wire format that
`Relation.save` cannot produce. Note that the ops docstring for `Relation.load`
contains a bug — it calls `get_secret` positionally, which raises `TypeError`.

## Who can write what

This constrains every reconcile step that touches a databag:

| Relation kind | Unit databag | App databag |
|---|---|---|
| non-peer | each unit reads/writes its own; reads all remote | **leader only** |
| peer | each unit reads/writes its own; **reads all** unit databags | **leader only** |

Any step writing the app databag must be guarded by `self.unit.is_leader()`, or it
raises on non-leaders. And note `leader_elected` must be in the reconciler's event
list, or a newly elected leader never publishes.


## Reconciling relations

The reconciler runs on every event, including events unrelated to a relation.
Every relation step must therefore tolerate:

- the relation not existing at all
- the relation existing with an empty databag
- the relation existing with a databag from an older charm version
- being the non-leader

```python
def _reconcile_dns_relations(self) -> None:
    """Publish our address to every dns consumer. Safe with zero relations."""
    if not self.unit.is_leader():
        return
    data = PiholeProviderData(address=self._bind_address)
    for relation in self.model.relations["dns"]:
        relation.save(data, relation.data[self.app])
```

No `if relation is None: return` guards at the top of `_reconcile` — iterate over
possibly-empty collections instead. A reconciler that bails early on a missing
relation stops converging everything downstream of it.

Two behaviours of `self.model.relations` worth knowing:

- **`self.model.relations["not-declared"]` raises `KeyError`**, it does not return
  `[]`. Only endpoints declared in `charmcraft.yaml` are valid keys. A declared
  endpoint with zero integrations *does* return `[]`, which is what "optional by
  default" relies on.
- **During `relation-broken`, the breaking relation is excluded from the list.**
  Your reconciler sees the world already without it. That is what you want, but it
  means you cannot inspect the departing relation from inside `_reconcile`.

For a single-relation endpoint, `self.model.get_relation("cos-agent")` returns
`Relation | None` and is more direct than indexing a list.

