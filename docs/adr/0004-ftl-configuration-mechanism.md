# ADR-0004: FTL Configuration Mechanism

**Status:** Accepted
**Date:** 2026-08-07
**Related:** [ADR-0003: Reconciler and Functional Core](0003-reconciler-and-functional-core.md), [ADR-0005: Status Semantics and Failure Handling](0005-status-semantics-and-failure-handling.md), [ADR-0006: Configuration Surface](0006-configuration-surface.md), [ADR-0007: Admin Password Handling](0007-admin-password-handling.md), [ADR-0009: Split the FTL API client out of `Pihole`](0009-ftl-api-client-module.md), [snap constraints §4](../snap-constraints.md)

---

## 1. Context

The snap exposes Pi-hole FTL configuration by prefixing each upstream key with
`ftl.` and proxying `snap set` through its configure hook into `pihole.toml`.
This works well — and then does not work at all for a third of the keys,
including the single most important one.

---

## 2. Problem Breakdown

### 2.1 66 of 166 FTL keys cannot be set, or even read

snapd validates configuration option names against

```
^(?:[a-z0-9]+-?)*[a-z](?:-?[a-z0-9])*$
```

which **rejects camelCase, uppercase, and underscores**. FTL v6 uses all three
freely. Verified:

```
$ snap set pihole-by-rajannpatel ftl.dns.listeningMode=LISTEN_ALL
error: ... (invalid option name: "listeningMode")   # exit 1
```

**`dns.listeningMode` is the one that matters.** Its default is `LOCAL`; a
Pi-hole serving a network needs `ALL`. So the charm's primary use case cannot be
configured through the snap's own configuration interface.

*(Note the value vocabulary is `LOCAL | SINGLE | BIND | ALL | NONE` — not
`LISTEN_LOCAL`/`LISTEN_ALL`, as an earlier draft of this ADR stated. Corrected
from the annotated `pihole.toml`.)*

### 2.2 One reachable key is a silent no-op

`hooks/configure:213` migrates `dns.dnssec` → `dns.dnssec_enabled`. FTL v6.7
still reads `dns.dnssec`, and `dnssec_enabled` is itself an invalid snapd option
name. Verified: `snap set ftl.dns.dnssec=true` returns **exit 0** and
`pihole.toml` still reads `dnssec = false`.

So key reachability is necessary but not sufficient, and **the exit code of
`snap set` is not evidence for any key.**

### 2.3 What `snap set` does well

The configure hook already diffs the requested value against the current TOML
(`local/runtime/pihole-config.sh:160-180`) and restarts FTL only when something
changed (`hooks/configure:263-267`). Verified by PID: setting the same value
twice does not restart. **Do not reimplement that diff.**

### 2.4 The bootstrap ordering constraint — discovered during the spike

This is what ultimately shapes the decision, and it was not visible when this
ADR was first written.

The packaged default `webserver.port = "80o,443os,[::]:80o,[::]:443os"` requests
TLS. FTL cannot generate its self-signed certificate inside this snap, and the
SSL failure **aborts the entire webserver** — including the plain-HTTP entries.
Result on a stock install: no port 80, no admin UI, **and no HTTP API**. See
[snap-constraints §5.1](../snap-constraints.md).

So `webserver.port` must be corrected **before the daemon first serves**, and the
HTTP API is unavailable until it is. Any mechanism that depends on the API cannot
be the *only* mechanism.

---

## 3. Approaches, evaluated against spike evidence

### A. `snap run --shell` then `pihole-FTL --config`

```sh
snap run --shell pihole-by-rajannpatel.pihole-ftl \
  -c '$SNAP/usr/bin/pihole-FTL --config dns.listeningMode ALL'
```

**Verified:** `snap run --shell <app> -c '...'` **does work non-interactively**
from a Juju hook, with `$SNAP_DATA` correctly set to the revision-versioned path.

**Pros** — uses the exact mechanism the snap uses internally
(`pihole-config.sh:178`); works before the daemon serves.
**Cons** — desynchronises snapd state from `pihole.toml`; requires a manual
`snap restart`, which drops DNS; one subprocess per key.

### B. The FTL v6 HTTP API — `PATCH /api/config`

**Verified working, including on both keys that defeat `snap set`:**

| Key | `snap set` | `PATCH /api/config` |
|---|---|---|
| `dns.listeningMode` (camelCase) | `invalid option name`, exit 1 | `listeningMode = "ALL"` ✓ |
| `dns.dnssec` | exit 0, value dropped | `dnssec = true` ✓ |

Additional verified properties:

- **Does not restart FTL.** PID identical after applying twice — so no DNS blip,
  and idempotency comes for free. This is *better* than `snap set`, which
  restarts whenever a value changes.
- Values **persist across an FTL restart**.
- Precise validation errors: `400` with
  `"hint": "dns.listeningMode: invalid option"` and
  `"hint": "dns.cache.size: not of type unsigned integer"`.

**But not via the `pihole api` CLI.** `api.sh`'s `GetFTLData` hardcodes `-X GET`
(line 237). A `PostFTLData` exists (line 264) but the `api` subcommand does not
use it, and there is no PATCH anywhere. The charm must issue its own HTTP
request.

**Cons** — requires the webserver up, so it cannot apply the bootstrap key
(§2.4); needs session handling; `cli_pw` rotates on every FTL restart.

### C. Run `pihole-FTL --config` directly from the host

Runs unconfined, outside the snap's mount namespace, with the wrong `$SNAP_DATA`.
**Rejected.** Recorded so it is not rediscovered as a shortcut.

---

## 4. Decision

**Split by bootstrap versus steady-state, not by snapd's regex.**

| Phase | Mechanism | Keys |
|---|---|---|
| **Bootstrap** — before the daemon first serves | `snap set ftl.*` | `webserver.port` (§2.4). Nothing else. |
| **Steady state** — daemon serving | `PATCH /api/config` | Everything else, all 166 keys. |

This is the key simplification the spike bought. The earlier draft of this ADR
routed each key by whether snapd's validation regex accepted it, which required a
`_is_snapd_safe_key()` predicate, a hard-coded exception for `dns.dnssec`, and
two mechanisms whose failure modes differed per key.

**`_is_snapd_safe_key` is deleted.** The reachable/unreachable distinction is an
accident of snapd's option-name validation and carries no meaning for the
workload. "Which keys must be applied before the API exists?" is a real question
with a stable, one-item answer.

Approach A is kept in reserve, not in the design: if a future key must be applied
before the daemon serves *and* is not snapd-reachable, A is the only option. No
such key exists today.

---

## 5. Design

### 5.1 The client

`ftl_api.py` owns a small HTTP client using stdlib `urllib.request` — no new
dependency. It talks to `http://127.0.0.1:<webserver-port>/api/`, and the charm
knows the port because it sets it (§4). (This client lived in `pihole.py` when
this ADR was written; ADR-0009 moved it.)

*(The `pihole api` wrapper discovers the URL by querying a CHAOS TXT record,
`dig +short -p <dns.port> chaos txt local.api.ftl @127.0.0.1`. That is a more
robust discovery mechanism and is worth adopting if we ever stop owning
`webserver.port`.)*

### 5.2 Authentication

```
POST /api/auth   {"password": "<cli_pw>", "totp": null}   ->  {"session": {"sid": ...}}
PATCH /api/config                                          header: sid: <sid>
DELETE /api/auth                                           header: sid: <sid>
```

`cli_pw` lives at `$SNAP_DATA/etc/pihole/cli_pw`, mode `0640`, 44 characters,
readable by root on the host.

**It is regenerated on every FTL restart — verified.** So the charm must read it
fresh on every use and must never cache it, in memory or otherwise.

**When no admin password is set (`pwhash = ""`, the default), the API accepts
unauthenticated writes.** That is a security problem the charm must close rather
than exploit; see [ADR-0007](0007-admin-password-handling.md). The client should
authenticate unconditionally and treat "auth not required" as a condition to fix,
not a convenience.

### 5.3 Payload shape

Nested, mirroring `pihole.toml`, not dotted:

```json
{"config": {"dns": {"listeningMode": "ALL", "dnssec": true}}}
```

Multiple keys can be sent in one request, which means one round trip per
reconcile rather than one per key. `compute` should therefore emit a single
`ApplyFtlConfig` outcome carrying the whole desired mapping.

### 5.4 Read-back is still mandatory

The API also lies, just differently:

| Input | Result |
|---|---|
| invalid value | `400`, precise hint — **good** |
| wrong type | `400`, precise hint — **good** |
| **unknown key** | **`200`, silently ignored** |

A typo in a key name returns success and does nothing. So non-negotiable #6
holds unchanged: **read `pihole.toml` back and diff against intent**, raising
`PiholeError(key, expected, actual)` on mismatch. `tomllib` is stdlib; we never
write TOML.

Map the `400` hint into the `BlockedStatus` message verbatim — it is already
phrased for a human.

### 5.5 Value serialisation

- Array keys take **JSON arrays**, natively, in the PATCH body. The CSV-to-JSON
  string conversion the old design needed for `snap set` disappears; the charm
  builds real JSON.
- Serialise from a **sorted tuple** so an unchanged config never produces a
  spurious diff.

### 5.6 `$SNAP_DATA`

Resolve through the `current` symlink. Verified that `$SNAP_DATA` inside the snap
is `/var/snap/pihole-by-rajannpatel/1348` — the revision-versioned path. **Never
hardcode a revision.**

---

## 6. Spike results

Run 2026-08-07 on an Ubuntu 26.04 LXD VM, snap rev 1348.

| Question | Answer |
|---|---|
| Does `pihole api` accept a method and a body? | **No.** GET-only; `GetFTLData` hardcodes `-X GET`. But a direct HTTP `PATCH` works. |
| Does `snap run --shell <app> -c '...'` work non-interactively? | **Yes**, with correct `$SNAP_DATA`. |
| Does `snap get` diverge after a non-`snap set` write? | **Yes.** `snap get ftl.dns.dnssec` → `false` while `pihole.toml` → `true`. |
| Is `cli_pw` readable, and does it survive a restart? | Readable (0640, 44 chars). **It rotates on every restart.** |
| Does `PATCH` restart FTL? | **No.** PID unchanged across two applications. |
| Does `PATCH` validate? | Yes for values and types (`400` + hint). **No** for unknown keys (`200`, ignored). |

---

## 7. Future Work (Out of Scope)

- **Config drift from the admin UI.** An operator changing a value in the web UI
  writes `pihole.toml` directly; the charm overwrites it on the next reconcile.
  Correct convergence, but surfacing the drift as an `ActiveStatus` message would
  be kinder. Now cheap, since `GET /api/config` returns the whole tree.
- **CHAOS TXT URL discovery** (§5.1), if the charm ever stops owning
  `webserver.port`.
- **Fixing the 66-key gap upstream.** The snap's configure hook could expose
  kebab-case aliases mapping to camelCase FTL keys. That would make `snap set`
  complete — but with the API path working, this is no longer on our critical
  path. Tracked in [BACKLOG.md](../BACKLOG.md).

---

## 8. Consequences

### Positive

- **One mechanism for 165 of 166 keys.** No routing predicate, no per-key
  exception, no `_is_snapd_safe_key`, and the `dns.dnssec` special case
  evaporates because the API applies it correctly.
- **No DNS blip on config change.** `PATCH` does not restart FTL, so the charm
  can converge configuration without interrupting the service it exists to
  provide. `snap set` could not offer this.
- **Idempotency is free**, which satisfies "safe to run twice" without a diff of
  our own.
- **Better error messages than we could have written.** FTL's `400` hints name
  the key and the violated constraint.
- Multiple keys per request means one round trip per reconcile.
- The bootstrap/steady-state split is a distinction with operational meaning, so
  it should stay stable as FTL's key set changes.

### Negative

- **snapd state is now permanently unreliable** as a view of FTL configuration.
  `snap get` shows only `webserver.port`; everything else lives in `pihole.toml`
  and nowhere else. This must be stated plainly in the README, because an
  operator's first instinct will be `snap get`.
- **Config application depends on the daemon serving HTTP.** The charm cannot
  apply steady-state config until port 80 is up, which couples configuration to
  webserver health and makes the install ordering load-bearing.
- **We own an HTTP client and session lifecycle** — auth, `sid` header,
  rotation of `cli_pw`, and cleanup — where before there was one `subprocess.run`.
- **An unknown key returns 200.** The read-back is not defence in depth; it is
  the only defence.
- If the snap ever changes `webserver.port` handling or the API's auth model, the
  charm's entire configuration path breaks at once. Concentrated risk, on an
  unofficial snap.
