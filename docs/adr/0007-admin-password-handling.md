# ADR-0007: Admin Password Handling

**Status:** Accepted
**Date:** 2026-08-08
**Accepted:** 2026-08-08
**Amended:** 2026-08-08 — §4.3's oracle table listed only `200` and `401`; the missing rows let an implementation misread `429` as a wrong password.
**Amended:** 2026-08-11 — §4.3 gained the settle window: `setpassword` reports success ~1s before FTL validates against the hash it just wrote, so a single `401` is not a verdict ([snap-constraints §7.2.5](../snap-constraints.md)).
**Related:** [ADR-0004: FTL Configuration Mechanism](0004-ftl-configuration-mechanism.md), [ADR-0005: Status Semantics and Failure Handling](0005-status-semantics-and-failure-handling.md), [ADR-0006: Configuration Surface](0006-configuration-surface.md), [ADR-0009: Split the FTL API client out of `Pihole`](0009-ftl-api-client-module.md), [snap constraints §5.2](../snap-constraints.md)

---

## 1. Context

Pi-hole's admin UI takes a password. On this snap that password is not a
convenience feature — it is the only thing standing between a deployment and a
remote, unauthenticated DNS hijack.

### 1.1 Without a password, the config API is open to the network

Verified 2026-08-07 on a stock install (Ubuntu 26.04 LXD VM, snap rev 1348), from
**a different host on the network, with no credentials**:

```
$ curl -X PATCH http://<pihole-ip>/api/config \
       -H 'Content-Type: application/json' \
       --data '{"config":{"dns":{"upstreams":["198.51.100.66"]}}}'
HTTP 200
```

Read back on the unit:

```
upstreams = [ "198.51.100.66" ] ### CHANGED, default = []
$ dig +short @127.0.0.1 example.com
;; communications error to 127.0.0.1#53: timed out
```

**Every device using that resolver had its DNS redirected by a stranger.** The same
request can rewrite any configuration key: disable blocking, alter `dns.hosts`,
change `webserver.acl`, or enable the DHCP server.

The cause is the packaged defaults `pwhash = ""` and `acl = ""`. Pi-hole v6 permits
unauthenticated API access when no password is set — upstream behaviour — but
upstream's installer *forces* a password during setup. The snap's documented
Quickstart (`snap install` then `snap start --enable`) never asks for one, and FTL
binds `0.0.0.0:80`.

**The charm opens this hole itself**, because it starts the daemon programmatically
and never runs the interactive wizard. Closing it is a correctness requirement, not
a feature.

### 1.2 The snap's own documentation contradicts itself on how to set a password

| Source | Says |
|---|---|
| `Reference: native-configuration` | Documents `ftl.webserver.api.password` as an ordinary settable/gettable `snap set` key, with examples that **echo the value back**, and **no warning**. |
| Operator runbook | *"Do not use `snap set` to change web passwords. Values passed through `snap set` can be recorded in snapd state. Set the password through Pi-hole FTL so it is hashed before storage: `sudo pihole setpassword`."* |

Observed behaviour settles it: setting `ftl.webserver.api.password` works — FTL
hashes it into `pwhash` — **but the plaintext persists in snapd state**, and
`GET /v2/snaps/pihole-by-rajannpatel/conf` returns it verbatim. Anyone with snapd
access can read the admin password.

The runbook is right. The reference page is dangerous.

---

## 2. Approaches for applying a password

### A. `snap set` the plaintext, then `snap unset` it

The hash is already in `pihole.toml`, so in principle the plaintext is no longer
needed.

**Pros** — declarative, and idempotent for free via the configure hook's diff.

**Cons**
- The plaintext is **written to snapd state first**, however briefly.
- Verified on `ftl.webserver.port` that `snap unset` does **not** revert a key to
  its FTL default — the last applied value stays. If that generalises, the cleanup
  step does not clean up.
- Two operations, either of which can fail, leaving the plaintext resident.

### B. `pihole setpassword`

```
pihole-by-rajannpatel.pihole setpassword '<pw>'    # -> [✓] New password set
```

**Pros**
- **The plaintext never reaches snapd state.** FTL hashes it before storage.
- Verified: **0.2–0.6 s**, exit 0, and it does **not restart FTL** — no DNS
  interruption.
- What the snap's own operator runbook prescribes.

**Cons**
- Imperative, so idempotency must be engineered (§4.3).
- The password appears in the process argument list for the duration of the call,
  visible to `ps` on the machine. Narrower than persistent snapd state, but not
  zero, and no stdin-based alternative is documented.

**Never `pihole -a -p '<pw>'`** — that is v5 syntax which prints usage and **exits
0**. A charm using it reports success having done nothing.

---

## 3. Decision

**Approach B, applied unconditionally by the charm. The charm owns the password;
the operator never supplies one.**

| Concern | Decision |
|---|---|
| Where the password comes from | The **charm generates it** on install |
| Where it is stored | A **charm-owned Juju secret** |
| How it is applied | `pihole setpassword`, before the daemon serves |
| Retrieval | `get-admin-password` action |
| Change | `rotate-admin-password` action — generates a new one |
| Config option | **None** |

Three things this deliberately rules out.

**No config option.** If the charm always generates a password, a `web-password`
option is a second mechanism for the same state, with precedence ambiguity between
them — and a config option is a permanent public API that cannot be withdrawn
(non-negotiable #4). One source of truth is better than two plus a rule.

**No password parameter on the rotate action.** An action parameter would place the
plaintext in the action's recorded params, visible via `juju show-task` and
`juju operations`, and in the operator's shell history. Generating is *more*
secure, not less. The action name says `rotate` rather than `set` precisely because
it accepts no value — "set" would invite the parameter.

**Approach A is rejected**, and recorded here because the snap's reference page will
keep suggesting it.

[ADR-0006](0006-configuration-surface.md) was amended to match: `web-password` is
listed there among the rejected options, not the accepted ones.

---

## 4. Design

### 4.1 The charm owns the secret

```python
secret = self.app.add_secret({"password": generated}, label=ADMIN_PASSWORD_LABEL)
```

- **App-owned**, so every unit can read it and it survives unit replacement.
- Created and updated **only on the leader**; `add_secret` and `set_content` raise
  on a non-leader.
- Retrieved by **label**, not by a stored ID, so nothing has to be remembered
  across hooks (no `StoredState` — ADR-0003 §2.8).
- Generated with `secrets.token_urlsafe(24)`.

Because the charm is the secret's *owner*, there is no `secret_changed` to observe
for it — that event is for consumers. Nothing extra needs wiring.

**NOT VERIFIED:** whether FTL imposes a maximum password length or a character
restriction. `token_urlsafe(24)` is well within any plausible limit, but if
generation ever becomes configurable this needs checking.

### 4.2 Ordering: before the daemon serves

The password must be applied **before, or as part of, the first successful start**.
There must be no window in which the daemon serves with `pwhash = ""`.

That places it in the Stage 1 install sequence, after the `webserver.port`
correction and before `StartFtl`:

```
install → free 53 → set webserver.port → set password → start → gate on API
```

(Install comes first so that a store failure cannot strand the host without a
resolver — see [ADR-0005 §2.9](0005-status-semantics-and-failure-handling.md).)

This ordering is a property of `compute`'s returned sequence, so it is asserted by
a pure test with no mocks ([ADR-0003](0003-reconciler-and-functional-core.md)).

### 4.3 Idempotency via `/api/auth`, not a hash comparison

Measured facts:

| Fact | Value |
|---|---|
| Does `pwhash` change when the *same* password is set twice? | **Yes** — random salt |
| `POST /api/auth` with the correct password | **`200`** |
| `POST /api/auth` with a wrong password | **`401`** |
| FTL verify primitive | **None** (`--perf` is only a BALLOON benchmark) |

The salt makes a `pwhash` diff useless — the stored hash differs on every write
even for an identical password. So the API is the oracle:

```
POST /api/auth {"password": <from the secret>}
   ->  200   already correct, do nothing
   ->  401   wrong password, apply `pihole setpassword`
   ->  429   session limit reached — CANNOT VERIFY, do nothing
   ->  other cannot verify, do nothing
```

**The third and fourth rows are load-bearing, and omitting them caused a real
defect.** An earlier implementation read this as a two-way branch and treated every
non-200 as "wrong password". FTL caps concurrent API sessions at 16
(`webserver.api.max_sessions`, itself unreachable via `snap set`) and answers `429`
when the pool is exhausted — so a busy charm reported *"Pi-hole rejects the password
this charm holds"* about a password that was perfectly correct. That message accuses
the operator of a security problem, which makes a false positive worse than silence.

**Only `401` means the credential is wrong.** Everything else means the oracle could
not answer, which is not a finding. See
[snap-constraints §7.2.4](../snap-constraints.md).

**And `401` only means it once it has stopped changing.** `pihole setpassword`
exits 0, and `pihole.toml` already holds the new `pwhash`, roughly a second before
FTL validates against it — measured at 401 on +0.91s, +1.20s and +1.49s, then 200
on +1.78s ([snap-constraints §7.2.5](../snap-constraints.md)). So the oracle is
consulted with a **bounded settle window**: poll for about 5 s at 0.5 s intervals,
and conclude `401` only if it persists for the whole window.

Three properties the window must keep:

- **A 200 returns immediately.** The healthy path — every reconcile on a converged
  machine — costs exactly one request and never waits.
- **`429` and everything else return immediately too.** They are capacity or
  ignorance, which no amount of waiting changes, and re-asking an exhausted session
  pool is the load that exhausted it.
- **A `401` lasting the whole window is still a rejection.** Patience must not mask
  a genuinely wrong password, or the §1.1 hole reopens silently.

The window applies to the steady-state check in `fetch` as well as to confirmation
after a write. A hook that applies a password and then reports status reads the
oracle again within that same second, and a transient `401` there would flap a
`BlockedStatus` accusing the operator of a security problem —
[ADR-0005 §2.8](0005-status-semantics-and-failure-handling.md): one spurious
`Blocked` masks every other status the handler adds.

Two consequences for anything that talks to this API: authenticate **once** and
reuse the `sid` rather than per attempt, and never let a polling loop authenticate
on every poll.

This reuses the HTTP client built for
[ADR-0004](0004-ftl-configuration-mechanism.md) and keeps BALLOON-SHA256 out of the
charm, which is the last place security-sensitive crypto should live.

**One ordering subtlety.** While `pwhash` is empty the API accepts *any* password,
so the oracle cannot distinguish "correct" from "unauthenticated" in that state. The
charm therefore reads `pwhash` from `pihole.toml` first:

- `pwhash == ""` → a password **must** be applied. This is also the §1.1 security
  assertion, so the charm needs this read regardless.
- `pwhash != ""` → consult the `/api/auth` oracle.

**NOT VERIFIED:** whether `POST /api/auth` with a *wrong* password returns `200`
while `pwhash` is empty. Moot given the branch above — but do not invert the order
and start relying on it.

### 4.4 Actions

Both are non-deferrable, so they get dedicated handlers — the official test for
"deserves its own handler". **Set `additionalProperties: false` explicitly**: the
default differs between Juju 3 and Juju 4.

```yaml
actions:
  get-admin-password:
    description: Retrieve the Pi-hole admin UI password.
    additionalProperties: false
  rotate-admin-password:
    description: >-
      Generate a new admin UI password, store it, and apply it to Pi-hole. Takes
      no parameters: a password passed as an action parameter would be recorded in
      the action's results and visible via `juju show-task`.
    additionalProperties: false
```

`get-admin-password` reads the charm-owned secret. Not snapd state, and not
`pihole.toml` — the latter holds only the hash.

`rotate-admin-password` generates, writes a new secret revision, applies it with
`setpassword`, and verifies with the §4.3 oracle before reporting success.

### 4.5 Verify every write

Two silent-failure paths, both requiring a read-back (non-negotiable #6):

- **`Secret.set_content` succeeds even when it will not take effect.** If the charm
  lacks permission or the secret is gone, the method returns normally and the unit
  errors at the *end* of the hook. Read the content back.
- **`setpassword` exits 0** on a v5-syntax invocation that does nothing. Confirm the
  hash changed in `pihole.toml` — the salt is random, so a genuine write always
  produces a different one — and confirm acceptance through the §4.3 oracle, with
  its settle window. Neither read-back replaces the other: the file proves the write
  happened, the oracle proves FTL honours it.

### 4.6 Related keys the charm does not manage

`ftl.webserver.api.app_pwhash`, `app_sudo`, and `totp_secret` are out of scope.
`totp_secret` is **write-only and cannot be read back**, so it could never be
reconciled; it would have to be an action. See [BACKLOG.md](../BACKLOG.md).

`cli_pw` is not an admin credential but matters elsewhere: it is a temporary CLI
password stored in clear at `$SNAP_DATA/etc/pihole/cli_pw` (mode `0640`) and
**regenerated on every FTL restart**. ADR-0004's HTTP client reads it fresh on
every use.

### 4.7 A regression test that must exist

Assert on the recorded argv that the charm **never** passes a password to
`snap set`, and never emits `pihole -a -p`. Both are cheap, and they guard against
someone later reading the snap's reference page and "simplifying" the
implementation.

---

## 5. Future Work (Out of Scope)

- **`webserver.acl`** as defence in depth. A restrictive ACL also blocks the admin
  UI the operator wants to reach, so it needs a design that separates "who may read
  the UI" from "who may write config" — probably driven by a Juju space rather than
  a config option.
- **Scheduled rotation** via `secret_rotate`, which is non-deferrable and would need
  its own handler.
- **Operator-supplied passwords.** If a real requirement appears — integrating with
  an external password manager, or restoring a known credential after a rebuild —
  the vehicle is a `type: secret` config option, which stores only a secret URI and
  never exposes the value in `juju config`. Note that Juju does **not** validate
  such a secret when the config is set, so a missing `juju grant-secret` surfaces as
  a `SecretNotFoundError` at reconcile time; the resulting `BlockedStatus` must name
  that command. A pydantic model holding an `ops.Secret` field also needs
  `arbitrary_types_allowed`.
- **TOTP / 2FA**, per §4.6.

---

## 6. Consequences

### Positive

- **A deployment is never remotely writable.** Generating a password when the charm
  starts the daemon closes the §1.1 hijack by construction, rather than depending on
  an operator to notice.
- **One source of truth.** No config option, no precedence rule, no way for two
  mechanisms to disagree.
- The password never enters snapd state, never appears in `juju config`, never
  appears in an action parameter, and never appears in shell history.
- **No crypto in the charm.** The `/api/auth` oracle replaces a BALLOON-SHA256
  comparison.
- Idempotency costs one HTTP request against a client we already have — the settle
  window is paid only by an answer that would otherwise accuse the operator — and
  `setpassword` does not restart FTL, so convergence never interrupts DNS.
- Retrieval by secret label means no state to carry across hooks.

### Negative

- **An operator cannot supply their own password.** Anyone with an existing
  credential-management workflow must adopt ours, or wait for §5. This is the
  deliberate cost of being opinionated, and it is the most likely source of a
  feature request.
- **Generating by default diverges from the workload's behaviour**, where no
  password means no authentication. An operator expecting the stock Pi-hole
  experience will meet a credential they did not set and must discover
  `get-admin-password`. The README must say so plainly.
- **`rotate-admin-password` invalidates existing sessions and any external
  automation** using the old password, with no way to pin a replacement value.
- The password is briefly visible in the process argument list during
  `setpassword`, so `ps` on the machine can see it. No stdin alternative is
  documented.
- **This ADR gates Stage 1**, pulling secret handling much earlier than a
  conventional charm would, and making the first functional milestone larger.
- **A genuinely wrong password is now slower to report.** Each consultation of the
  oracle spends the full §4.3 window — about 5 s and ~11 requests — before saying
  so, and `fetch` runs twice per hook. That is deliberate: the delay lands only on
  the path that raises a security alarm, and none of those requests holds a session,
  because a `401` issues none. **NOT VERIFIED:** whether FTL rate-limits or delays
  repeated failed logins on `/api/auth`. If it does, the interval is the knob.
- Leader-only secret writes are a latent multi-unit concern. Harmless for a
  single-unit charm, but it must be revisited alongside any peer relation.
