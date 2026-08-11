# Pi-hole snap: verified constraints

**Snap:** `pihole-by-rajannpatel`
**Source:** [`rajannpatel/snap-pi-hole@main`](https://github.com/rajannpatel/snap-pi-hole)
**Wiki:** https://github.com/rajannpatel/snap-pi-hole/wiki
**Last verified:** 2026-08-07

Reference document. Every claim here was verified by reading the snap source or
by running the snap — not inferred from documentation. Anything unverified is
labelled **NOT VERIFIED**, and that distinction is load-bearing: the charm's
design depends on knowing which facts are solid.

Cited as authority by [ADR-0001](adr/0001-charm-scope-and-specification.md)
through [ADR-0008](adr/0008-cos-integration.md).

---

## 1. Identity and supply chain

| | |
|---|---|
| snap-id | `2jrR40wTkMgV4rH1ixNcpRNYCINMDe6t` |
| Publisher | `rajannpatel` — **unproven**, not a verified publisher |
| License | MIT |
| Confinement | `strict` |
| Base | **`core26`** |
| Channels | **only `latest/stable` and `latest/edge`** — no tracks, no candidate, no beta |
| Packages | Pi-hole **v6** — Core v6.4.3, Web v6.6, FTL v6.7 |

Three supply-chain facts the charm cannot design around:

1. **Unofficial.** The store listing itself says: *"The Pi-hole project
   maintainers do not yet support snap-based installations."* Workload bugs have
   no upstream escalation path.
2. **No version line to pin.** `--channel` distinguishes only stable from edge.
   Reproducibility requires `--revision`, which then stops receiving security
   updates. This is a genuine trade-off to expose to the operator, not one the
   charm can resolve.
3. **Architecture coverage is uneven.** `amd64`/`arm64` build on GitHub-hosted
   runners and feed the security gate. `armhf`/`ppc64el`/`riscv64`/`s390x` build
   on Launchpad, decoupled from that gate. The project's own words: *"Runtime
   validation depth can differ by architecture."*

**Verified 2026-08-07:** installs and runs on Ubuntu **26.04** (snapd
2.76+ubuntu26.04.3) and **24.04** (snapd 2.76+ubuntu24.04.1). On 26.04 the host
and the snap's `core26` base align natively.

**Verified limitation — Juju-created LXD containers on 26.04.** Installing this
snap fails there: snapd attempts a kernel squashfs mount of `snapd_*.snap` and
fails with `wrong fs type, bad option, bad superblock`, because the container has no
`/dev/loop*`. Note the scope: a plain `lxc launch ubuntu:26.04` **succeeds**,
because it arrives with the `snapd` snap already seeded and mounted
`fuse.snapfuse`. What breaks is the *bootstrap* mount of the `snapd` snap, which a
Juju container must perform. Not a property of 26.04, and not of this snap. In a 26.04 LXD **VM**
(`virt-type=virtual-machine`) everything works. This is snapd/26.04 ecosystem lag,
not a defect in this snap — but it means **integration tests must use VMs**. See
[ADR-0002 §2.2.2](adr/0002-tech-stack-and-repo-architecture.md).

**NOT VERIFIED:** whether `base: core26` resolves on Ubuntu 22.04 or older, or with
snapd older than 2.76.

---

## 2. Services

```
Service                             Startup   Current   Notes
pihole-by-rajannpatel.gravity-sync  enabled   inactive  timer-activated
pihole-by-rajannpatel.pihole-ftl    disabled  inactive  install-mode: disable
```

### 2.1 `pihole-ftl`

`snapcraft.yaml:238-247`:

```yaml
pihole-ftl:
  command: bin/launcher-ftl
  daemon: simple
  install-mode: disable
  restart-condition: on-failure
  refresh-mode: endure
  stop-timeout: 60s
```

Each line has a charm consequence:

- **`install-mode: disable`** — the daemon does **not** start after install. A
  charm that only calls `snap.ensure()` installs a Pi-hole that never runs. The
  charm must explicitly `snap start --enable pihole-by-rajannpatel.pihole-ftl`.
  The snap docs confirm this is deliberate: *"This prevents the service from
  starting before port 53 is available."*
- **`restart-condition: on-failure`** + the launcher no longer pre-checking port
  53 (`launcher-ftl.sh:20-24`: *"If port 53 is occupied, FTL will log a clear
  EADDRINUSE error and crash."*) = an **indefinite crash loop**, not a clean
  failure. Check the port before starting.
- **`refresh-mode: endure`** — DNS survives `snap refresh`. The old process keeps
  serving until the new revision's daemon starts.
- Unit name: `snap.pihole-by-rajannpatel.pihole-ftl.service`.

### 2.2 `gravity-sync`

`snapcraft.yaml:316-318`: `daemon: oneshot`, `timer: sun,03:00~05:00`, enabled by
default.

**The schedule is not configurable through the snap.** The configure hook rejects
any `timer.*` key and aborts the whole transaction (`hooks/configure:131-192`),
because *"Snap application timers are static: snapd has no mechanism to change
them at runtime."* Changing it requires a host-side systemd drop-in that the
**charm** must write:

```
/etc/systemd/system/snap.pihole-by-rajannpatel.gravity-sync.timer.d/override.conf
```

Note `OnCalendar=` must be cleared before being set, or the packaged value
remains in effect.

---

## 3. Interfaces

Auto-connected on install — enough for DNS and the web UI with no intervention:
`network`, `network-bind`, `network-observe`, `shared-memory` (private).

Require manual `snap connect`:

| Plug | Buys you |
|---|---|
| `network-control` | DHCP server mode |
| `firewall-control` | DHCP server mode |
| `system-observe` | per-process DNS attribution (`/proc/<pid>/comm`) |
| `time-control` | NTP client (`ntp.sync`, needs `CAP_SYS_TIME`) |
| `process-control` | `misc.nice` (needs `CAP_SYS_NICE`) |
| `hardware-observe` | hardware info on the diagnostics page |
| `mount-observe` | filesystem info on the diagnostics page |

Verified empirically: without `time-control`, `process-control`, and
`system-observe`, `FTL.log` emits `CAP_SYS_TIME required`, `CAP_SYS_NICE
required`, and `Could not fopen("/proc/<pid>/comm")`, plus AppArmor `DENIED`
lines in `dmesg`. Connecting those three and restarting makes all of them
disappear.

`snap connect` is **idempotent** — reconnecting a connected plug is a no-op, so
it is safe on every reconcile.

The snap's own security docs warn: *"Store auto-connection policy and local
`--dangerous` installs can produce different interface states."* So read
`snap connections` back rather than assuming.

---

## 4. Configuration via `snap set`

The snap proxies snapd config to `pihole-FTL --config`. Every FTL key is exposed
as `ftl.<upstream.key>`, written into `pihole.toml`.

**The configure hook already diffs against the TOML before applying**
(`local/runtime/pihole-config.sh:160-180`):

```sh
if [ -n "$toml_val" ] && [ "$norm_val" = "$norm_toml_val" ]; then
    continue
fi
"$pihole_ftl_bin" --config "$key" "$val" >/dev/null 2>&1 || {
    echo "Error applying ftl.$key=$val" >&2
    return 1
}
```

FTL is restarted **only if a value actually changed** (`hooks/configure:263-267`).
Verified by PID: setting the same value twice does not restart. So `snap set` is
genuinely idempotent and safe on every `config-changed` — **do not reimplement
the diff.**

`snap get` returns `has no configuration` until the daemon has started once;
`bin/config-sync` then syncs all 166 `pihole.toml` keys into snapd state.

### 4.1 Trap: 66 of 166 FTL keys are unreachable

snapd validates option names against

```
^(?:[a-z0-9]+-?)*[a-z](?:-?[a-z0-9])*$
```

([snapd `overlord/configstate/config/helpers.go`](https://github.com/canonical/snapd/blob/master/overlord/configstate/config/helpers.go)),
which **rejects camelCase, uppercase, and underscores**. FTL v6 uses all three
freely. This is a snapd limitation, not a snap bug, and there is **no workaround
through `snap set`** — the keys cannot even be *read*.

```
$ snap set pihole-by-rajannpatel ftl.dns.listeningMode=LISTEN_ALL
error: ... (invalid option name: "listeningMode")
```

The worst casualty is **`dns.listeningMode`**. Its values are
`LOCAL | SINGLE | BIND | ALL | NONE` (verified from the annotated `pihole.toml`, and
**not** `LISTEN_LOCAL`/`LISTEN_ALL` as previously recorded here). The default is
`LOCAL`; a Pi-hole serving a network needs `ALL`, so **the charm's primary use case
cannot be configured through `snap set` at all.** Resolved by using the HTTP API —
see [ADR-0004](adr/0004-ftl-configuration-mechanism.md).

Other notable unreachable keys: `dns.queryLogging`, `dns.blockTTL`,
`dns.revServers`, `dns.cnameRecords`, `dns.hostRecord`, `dns.bogusPriv`,
`dns.domainNeeded`, `dns.expandHosts`, `dns.rateLimit.count`,
`dns.rateLimit.interval`, `dns.reply.host.IPv4`/`IPv6`, `dns.specialDomains.*`,
`dhcp.leaseTime`, `dhcp.rapidCommit`, `dhcp.multiDNS`,
`dhcp.ignoreUnknownClients`, all of `resolver.*`, `database.maxDBdays`,
`database.DBinterval`, `database.useWAL`, `webserver.api.max_sessions`,
`webserver.api.totp_secret`, `webserver.api.allow_destructive`,
`webserver.serve_all`, `misc.etc_dnsmasq_d`, `misc.dnsmasq_lines`,
`misc.extraLogging`, `misc.readOnly`, `misc.delay_startup`.

Reachable keys covering the essentials: `dns.upstreams`, `dns.port`,
`dns.interface`, `dns.hosts`, `dns.domain.name`, `dns.cache.size`,
`dns.blocking.active`, `dns.blocking.mode`, `dhcp.active`, `dhcp.start`,
`dhcp.end`, `dhcp.router`, `dhcp.netmask`, `dhcp.ipv6`, `dhcp.logging`,
`dhcp.hosts`, `webserver.port`, `webserver.domain`, `webserver.acl`,
`webserver.threads`, `webserver.api.password`, `webserver.api.pwhash`,
`webserver.tls.cert`, `webserver.session.timeout`, `ntp.*`, `misc.privacylevel`,
`files.log.*`.

### 4.2 Trap: `ftl.dns.dnssec` is a silent no-op

```
$ snap set pihole-by-rajannpatel ftl.dns.dnssec=true   # exit 0, no warning
$ grep dnssec .../pihole.toml                          # dnssec = false — UNCHANGED
```

`hooks/configure:213` migrates `dns.dnssec` → `dns.dnssec_enabled`:

```
{"old": "dns.dnssec", "new": "dns.dnssec_enabled"}
```

FTL v6.7 still reads `dns.dnssec`, and `dnssec_enabled` is not a valid snapd
option name (underscore). The value is dropped and `snap set` reports success.

**Generalise this: the exit code of `snap set` is not evidence.** Always read
back, from `pihole.toml` or from snapd state.

### 4.3 Trap: array values are JSON, not CSV

The wiki is explicit: *"Comma-separated lists are not valid FTL v6 values."*
`dns.upstreams`, `dns.hosts`, `dns.revServers`, and `dhcp.hosts` all take JSON
array strings.

### 4.4 Trap: DHCP keys have a mandatory order

```
$ snap set ... ftl.dhcp.active=true            # with an empty pool
error: ... (run hook "configure": Error applying ftl.dhcp.active=true)
        # underlying: "DHCP start address is not valid" (exit 3)
```

Write `dhcp.start`, `dhcp.end`, `dhcp.router` **before** `dhcp.active`. And if
the bind on port 67 fails, FTL does not degrade — it crash-loops via
`restart-condition: on-failure`.

**NOT VERIFIED:** whether DHCP works end-to-end under strict confinement. In
testing the bind failed with `EADDRINUSE` (LXD's dnsmasq on `lxdbr0:67`), which
is a port conflict rather than an AppArmor denial — so confinement does not
*appear* to be the blocker, but it was never proven on a host with 67 free.

### 4.5 Known snap bug

`bin/snap-check:106` suggests `snap set <snap> webserver.port=8080` — **missing
the `ftl.` prefix**. The configure hook only reads the `ftl` and `timer`
namespaces (`configure:137,223`), so that command is accepted into snapd state
and does nothing. Always use `ftl.webserver.port`.
*(Verified by code inspection, not at runtime.)*

---

## 5. Ports

Verified with `ss -tulpn`:

| Port | Use | Notes |
|---|---|---|
| 53 tcp+udp | DNS | `ftl.dns.port` |
| 80 tcp | admin UI + API | default `webserver.port = "80o,443os,[::]:80o,[::]:443os"`; the `o` suffix means *optional* — it does not fail if taken |
| 443 tcp | HTTPS | the `s` suffix; needs `webserver.tls.cert` |
| **123 udp** | **NTP server — active by default** | `ntp.ipv4.active`/`ntp.ipv6.active` default `true`. Unexpected attack surface for a DNS appliance. Both keys are reachable, so the charm can decide. |
| 67 / 546 udp | DHCP / DHCPv6 | only when `dhcp.active=true` |
| 4711 | **not used** | that was FTL v5's telnet API. v6 serves the API over HTTP on `webserver.port`. |

### 5.1 The webserver does not start at all on a stock install

**Verified 2026-08-07** (Ubuntu 24.04 container and 26.04 VM, snap rev 1348). This
is the single most consequential defect for the charm, and it is documented
nowhere upstream.

The packaged default `webserver.port = "80o,443os,[::]:80o,[::]:443os"` requests
TLS via the `s` suffix. FTL tries to auto-generate `/etc/pihole/tls.pem`, fails,
and the SSL context error **aborts the entire webserver — including the plain-HTTP
`80o` entries, despite `o` meaning optional**:

```
ERROR: Generation of SSL/TLS certificate /etc/pihole/tls.pem failed!
ERROR: Start of webserver failed! Web interface will not be available!
ERROR:        Error: Error initializing SSL context (error code 3.0)
```

Net effect: **no port 80, no port 443, no admin UI, and no HTTP API.** DNS works
normally, which masks the failure completely.

Certificate generation fails for **both** key types, in the same call, after key
generation succeeds:

```
$ pihole-FTL --gen-x509 /etc/pihole/tls.pem pi.hole
ERROR: mbedtls_x509write_crt_pem (CA) returned -20352   # -0x4F80 ECP_BAD_INPUT_DATA
$ pihole-FTL --gen-x509 /etc/pihole/tls.pem pi.hole rsa
ERROR: mbedtls_x509write_crt_pem (CA) returned -16512   # -0x4080 RSA_BAD_INPUT_DATA
```

Ruled out: **not** confinement (zero AppArmor denials), **not** file permissions
(`/etc/pihole` is writable inside the sandbox), **not** missing libraries (all
three `libmbed*` resolve from `$SNAP/usr/lib/`), **not** missing entropy
(`/dev/urandom` present), **not** a port conflict. mbedTLS works for TLS
*connections* (`--tls-ciphers` enumerates suites) but cannot *emit* a certificate.
**Inferred, not proven:** the bundled mbedTLS lacks a working x509/PEM write path.

**Fix the charm applies**, verified to bind port 80 immediately and to avoid the
failure entirely when applied *before* first start:

```
snap set pihole-by-rajannpatel ftl.webserver.port="80o,[::]:80o"
```

`webserver.port` is a snapd-reachable key, which is why it is the sole bootstrap
key in [ADR-0004 §4](adr/0004-ftl-configuration-mechanism.md).

**`snap-check` does not detect this.** With the webserver dead it returns exit `0`
and its output never mentions the webserver or port 80.

Reported upstream — see `snap-issue-webserver-tls.md` in the repository root.

### 5.2 With no admin password, the config API is open to the network

**Verified 2026-08-07** from a *different host*, with *no credentials*:

```
$ curl -X PATCH http://<pihole-ip>/api/config -H 'Content-Type: application/json' \
       --data '{"config":{"dns":{"upstreams":["198.51.100.66"]}}}'
HTTP 200
```

The value landed in `pihole.toml` and DNS resolution for the whole network broke.
Any configuration key can be rewritten this way.

Cause: the defaults `pwhash = ""` and `acl = ""`. Pi-hole v6 permits
unauthenticated API access when no password is set (upstream behaviour), but
upstream's installer forces a password during setup. The snap's documented
Quickstart never does, and FTL binds `0.0.0.0:80`.

**The charm must close this before the daemon serves** — see
[ADR-0007 §1.3](adr/0007-admin-password-handling.md).

---

## 6. Paths

```
$SNAP        = /snap/pihole-by-rajannpatel/current
$SNAP_DATA   = /var/snap/pihole-by-rajannpatel/current    # revision-versioned!
$SNAP_COMMON = /var/snap/pihole-by-rajannpatel/common
```

| What | Host path |
|---|---|
| Main config | `$SNAP_DATA/etc/pihole/pihole.toml` |
| Blocklist DB | `$SNAP_DATA/etc/pihole/gravity.db` |
| Query DB | `$SNAP_DATA/etc/pihole/pihole-FTL.db` |
| Generated dnsmasq conf | `$SNAP_DATA/etc/pihole/dnsmasq.conf` |
| Local CLI password | `$SNAP_DATA/etc/pihole/cli_pw` (mode 0640) |
| Logs | `$SNAP_COMMON/var/log/pihole/{FTL,pihole,webserver,gravity-init}.log` |
| FTL daemon binary | `$SNAP/usr/bin/pihole-FTL` |
| Upstream `pihole` script | `$SNAP/opt/pihole/pihole` |
| Host-exposed wrapper | `/usr/local/bin/pihole` (via `layout: bind-file`) |

**`$SNAP_DATA` is versioned per revision** (`current` → `1348`); snapd copies the
tree on refresh. Resolve through `current`, **never hardcode the revision**.

FTL runs **as root** inside the sandbox, not as a `pihole` user.

---

## 7. Commands

The declared alias `pihole` **does not auto-register** — that needs a store
assertion this snap does not have. `snap aliases | grep pihole` is empty after
install. Use the fully qualified `pihole-by-rajannpatel.pihole`, or run
`snap alias pihole-by-rajannpatel.pihole pihole` during install.

Apps: `pihole`, `pihole-ftl`, `snap-check`, `snap-setup`, `snap-debug`, `sqlite3`,
`gravity-sync`.

### 7.1 Commands that lie

```
$ pihole-by-rajannpatel.pihole -a -p 'secret'
Usage: pihole [options] ...
exit=0                                  # <-- did nothing

$ pihole-by-rajannpatel.pihole restartdns
Usage: pihole [options] ...
exit=0                                  # <-- did nothing
```

Both are v5 syntax. **A charm that ports v5 logic will report `active/idle`
having done absolutely nothing.**

### 7.2 v6 equivalents (verified)

| Intent | Correct v6 command |
|---|---|
| Set password | `pihole setpassword '<pw>'` (see [ADR-0007](adr/0007-admin-password-handling.md)) |
| Restart DNS | `snap restart pihole-by-rajannpatel.pihole-ftl` |
| Reload lists + flush cache | `pihole-by-rajannpatel.pihole reloaddns` |
| Reload lists, keep cache | `pihole-by-rajannpatel.pihole reloadlists` |
| Status | `pihole-by-rajannpatel.pihole status` |
| Update gravity | `pihole-by-rajannpatel.pihole -g` |
| Health check | `pihole-by-rajannpatel.pihole snap-check` |
| Query the API | `pihole-by-rajannpatel.pihole api <endpoint>` → JSON |

`pihole api dns/blocking` → `{"blocking":"enabled","timer":null,"took":...}` is
the best readiness signal available — **but only once `webserver.port` has been
corrected (§5.1); on a stock install it returns
`Communication error. Is FTL running?`**

### 7.2.1 The `api` subcommand is GET-only

`opt/pihole/api.sh`: `GetFTLData` hardcodes `-X GET` (line 237). A `PostFTLData`
exists (line 264) but the `api` subcommand never calls it, and there is no PATCH
anywhere. To *write* configuration over HTTP the charm must issue its own request;
see [ADR-0004 §5](adr/0004-ftl-configuration-mechanism.md).

The wrapper discovers the API URL by a CHAOS TXT DNS query —
`dig +short -p <dns.port> chaos txt local.api.ftl @127.0.0.1` — which is a more
robust mechanism than assuming a port.

### 7.2.2 `cli_pw` rotates on every FTL restart

`$SNAP_DATA/etc/pihole/cli_pw`, mode `0640`, 44 characters, readable by root on the
host. **Verified: the value changes across `snap restart`.** Any code that
authenticates against the API must re-read it on every use and must never cache it.

### 7.2.3 `PATCH /api/config` does not restart FTL

Verified: FTL's PID is unchanged after applying configuration twice. So writes via
the API cause no DNS interruption and are idempotent for free — unlike `snap set`,
which restarts whenever a value changes.

Validation is good for values and types (`400` with a precise `hint`) and **absent
for key names: an unknown key returns `200` and is silently ignored.**

### 7.2.4 The API caps concurrent sessions at 16, and answers 429

Verified 2026-08-07 on a live unit:

```
webserver.api.max_sessions = 16        # pihole.toml default

$ # POST /api/auth repeatedly without logging out
attempt 15 -> HTTP 429
```

Three consequences for the charm:

- **`429` means "no session slots", not "wrong password".** Any code that treats a
  non-200 from `/api/auth` as a credential failure will raise a false security
  alarm. Only `401` means the password is wrong.
- **Any polling loop that authenticates per attempt will exhaust the pool.**
  Authenticate once and reuse the `sid`. `DELETE /api/auth` is best effort and FTL
  does not free slots fast enough to keep up with a tight loop.
- **`max_sessions` cannot be raised via `snap set`** — the underscore makes it one
  of the unreachable keys (§4.1). Reduce consumption instead, or use
  `PATCH /api/config`.

### 7.2.5 `setpassword` reports success ~1s before the password works

Measured 2026-08-07 on a live unit. `pihole setpassword` exits **0** and prints
`[✓] New password set`, but FTL keeps validating against the *old* hash for about a
second:

```
setpassword rc=0 after 0.86s: [✓] New password set
  +0.91s  POST /api/auth -> 401
  +1.20s  POST /api/auth -> 401
  +1.49s  POST /api/auth -> 401
  +1.78s  POST /api/auth -> 200   <- first acceptance
```

FTL writes `pihole.toml` synchronously — the `pwhash` read-back passes immediately —
but does not reload its in-memory hash until slightly later.

**So a single `401` straight after a write is not evidence that the password is
wrong.** Any confirmation of a freshly applied password needs a bounded settle
window; concluding `PasswordRejected` on the first attempt produces a false security
alarm about a credential that is correct.

This is the same shape as every other trap in this file: **the workload reports
success before the state it claims to have produced is observable.**

### 7.2.6 Readiness requires authentication once a password is set

`GET /api/dns/blocking` returns **`401`** with no `sid` when `pwhash` is non-empty.
With `pwhash = ""` it answers unauthenticated — which is the §5.2 hole. So a charm
that sets a password (as it must) cannot use an unauthenticated readiness probe.

### 7.2.7 `snap unset` does not revert a key to its default

Verified on `ftl.webserver.port`: after `snap unset`, `pihole.toml` retains the
last applied value rather than returning to the FTL default. Relevant to any design
that assumes `unset` undoes a `set`.

### 7.3 `snap-check` exit codes

Semantic, per `bin/snap-check:39,55,92,97,141`:

- `0` — OK
- `1` — config error (a required plug is disconnected)
- `2` — runtime error (port conflict)

It checks plugs, ports 53/80/67/546, and AppArmor denials.

**The wiki documents no exit codes at all** — see §9. Pin this behaviour with an
integration test rather than trusting it silently.

Subcommands the launcher rejects with exit 1 (`launcher-pihole.sh:53-62`): `-up`,
`updatePihole`, `updatechecker`, `uninstall`, `checkout`. `-r`/`repair`
redirects to `snap-setup`.

Without root, only `""`, `-h`, `--help`, `help`, `-v`, `--version`, `version`,
`status`, `-q`, `query`, `snap-check` are allowed.

---

## 8. Host state the snap cannot manage

### 8.1 systemd-resolved and port 53

**Entirely the charm's job.** Strict confinement prevents the snap from writing to
`/etc/systemd/`. The snap's own architecture docs state it plainly: *"A strictly
confined snap cannot safely stop host services such as systemd-resolved or
dnsmasq to free port 53. For that reason, the Pi-hole daemon is installed
disabled."*

```
mkdir -p /etc/systemd/resolved.conf.d
printf '[Resolve]\nDNS=127.0.0.1\nDNSStubListener=no\n' \
  > /etc/systemd/resolved.conf.d/pihole.conf
systemctl restart systemd-resolved
```

`bin/snap-check:86-92` detects the conflict (`127.0.0.53:53`) and prints exactly
this remediation with exit 2.

### 8.2 The snap's `remove` hook cannot undo it

`snap/hooks/remove` reads, in full:

> *"Strict confinement prevents the snap from modifying host systemd settings or
> drop-ins due to AppArmor write/delete restrictions (though it can read the
> file, so the check passes). We cannot delete the drop-in conf ourselves, nor
> can we restart systemd-resolved. Instead, we print a warning and provide
> instructions for the operator."*

It only prints:

```
sudo sh -c 'rm -f /etc/systemd/resolved.conf.d/pihole.conf && systemctl restart systemd-resolved'
```

**Therefore the charm's `remove` handler is the only thing standing between an
operator and a machine with no DNS at all.** This single fact drives the status
strategy in [ADR-0005](adr/0005-status-semantics-and-failure-handling.md): a unit
in error state needs `--force` to remove, and `--force` skips the cleanup.

---

## 9. Where the official docs are wrong or absent

The wiki contradicts itself and observed behaviour. Recorded here so the charm's
choices are traceable.

| Topic | Conflict | Resolution |
|---|---|---|
| DHCP pool keys | `Reference: native-configuration` → `ftl.dhcp.start/end/router`. `How-to: configure-DHCP` → `ftl.dhcp.ipv4.range.start/end/router`. **Both cannot be right.** | Use the empirically verified set (`dhcp.start`, `dhcp.end`, `dhcp.router`, `dhcp.netmask`), but **re-verify** before implementing DHCP. |
| Admin password | Operator runbook: *"Do not use `snap set` to change web passwords."* `Reference: native-configuration` documents `ftl.webserver.api.password` as an ordinary settable key **with no warning**. | Follow the runbook. See [ADR-0007](adr/0007-admin-password-handling.md). |
| `snap-check` exit codes | **Not documented anywhere in the wiki.** | Use the source-verified codes in §7.3 and pin them with a test. |
| Metrics / Prometheus | **No mention anywhere** in 25 wiki pages — no exporter, no `/metrics`, no observability integration. | Nothing exists to wire up. See [ADR-0008](adr/0008-cos-integration.md). |
| Content slots for logs | — | **Verified by grep: `snapcraft.yaml` contains no `slots:` key at all.** `COSAgentProvider(log_slots=...)` is therefore impossible; forward logs by path. |

Also documented by the snap project as a self-declared weakness: the v5→v6 config
migration *"has not been fully tested for this snap workflow."* Do not automate
on top of it.

---

## 10. Readiness is not the same as active

`launcher-ftl.sh:67-121`: on first boot, if `gravity.db` is missing, the launcher
runs `pihole -g` synchronously to create the schema, inserts the default adlist,
then **forks a background child** that waits up to 90s for FTL to answer DNS
(`dig @127.0.0.1 . NS`) before downloading the list.

So `pihole-ftl` reports `active` long before blocking works. **The charm must not
go `active/idle` on `snap services` output.** Gate on `pihole api dns/blocking`
responding, and optionally on `gravity.db` exceeding a sane size.

---

## 11. Required install sequence

1. Free port 53 (systemd-resolved drop-in) — **charm's job**, §8.1.
2. `snap install pihole-by-rajannpatel`.
3. `snap connect` the manual plugs that apply.
4. `snap alias`, or commit to the fully qualified command name.
5. Apply config: `snap set ftl.*` for reachable keys, a fallback for the rest.
6. `snap start --enable pihole-by-rajannpatel.pihole-ftl`.
7. Poll readiness via the HTTP API, not via systemd.

**The charm deliberately swaps steps 1 and 2.** This list is what the snap's own
documentation prescribes, and it is recorded here unchanged because that is what
this file is for. But the *binding* constraint is §2.1's — port 53 must be free
before the **daemon starts**, and `install-mode: disable` means installing does not
start it. So the charm installs first, which keeps the host's resolver working
during the store fetch and means a store failure cannot strand a machine with
`DNSStubListener=no` and no Pi-hole. See
[ADR-0005 §2.9](adr/0005-status-semantics-and-failure-handling.md).

---

## 12. Blocklists are not declarative

Adlists live in the `adlist` table of `gravity.db`, not in config. The launcher
seeds Steven Black's list on first boot with `INSERT OR IGNORE`
(`launcher-ftl.sh:67-79`). Managing them requires the v6 HTTP API (`/api/lists`)
or `pihole-FTL sqlite3` plus `pihole -g`. Neither is idempotent or transactional.
Deferred — see [BACKLOG.md](BACKLOG.md).
