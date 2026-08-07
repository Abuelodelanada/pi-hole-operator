---
name: pihole-snap
description: >-
  Use for anything touching the pihole-by-rajannpatel snap — snap set / snap get
  keys, snap connect plugs, snap services, pihole CLI subcommands, ports,
  pihole.toml, gravity.db, $SNAP_DATA paths, systemd-resolved port 53 conflicts,
  readiness checks, or DHCP. Read this BEFORE writing any code that shells out
  to snap or pihole, because several of those commands return exit 0 without
  doing anything.
metadata:
  verified: "2026-08-06"
  snap-revision: "1348 (amd64, latest/stable)"
  method: "installed on Ubuntu 24.04 / snapd 2.76.1, squashfs inspected, runtime-verified"
---

# The `pihole-by-rajannpatel` snap

Everything here was verified by installing the snap, running it, and inspecting
the squashfs — not inferred from docs. Where something was not verified it says
**NOT VERIFIED**. Treat that distinction as load-bearing.

Source repo is available as the `snap-pi-hole` reference
(`rajannpatel/snap-pi-hole`). Read `snap/snapcraft.yaml`, `meta/hooks/configure`,
`bin/launcher-ftl`, `bin/launcher-pihole`, `bin/snap-check`, and
`bin/pihole-config.sh` when this file is not specific enough.

## Identity

| | |
|---|---|
| Name | `pihole-by-rajannpatel` |
| snap-id | `2jrR40wTkMgV4rH1ixNcpRNYCINMDe6t` |
| Publisher | `rajannpatel` — **unproven**, not a verified publisher |
| License | MIT |
| Confinement | `strict` |
| Grade | `stable` |
| Base | **`core26`** |
| Epoch | `0` |
| Channels | **only `latest/stable` and `latest/edge`**. No candidate, no beta, no tracks. |
| Architectures | amd64, arm64, armhf, ppc64el, s390x, riscv64 |
| Packages | Pi-hole **v6** — Core v6.4.3, Web v6.6, FTL v6.7 |

Consequences for the charm:

- **There is no version line to pin.** `--channel` buys you nothing beyond
  stable/edge. Reproducibility requires pinning `--revision`, which then never
  gets security updates automatically. Surface this trade-off explicitly.
- The snap is explicitly **unofficial**: *"The Pi-hole project maintainers do not
  yet support snap-based installations"*.
- `base: core26` means snapd must be able to fetch `core26`. Verified on Ubuntu
  24.04 + snapd 2.76.1. **NOT VERIFIED** on 22.04/20.04 or older snapd.

## Services

```
Service                             Startup   Current   Notes
pihole-by-rajannpatel.gravity-sync  enabled   inactive  timer-activated
pihole-by-rajannpatel.pihole-ftl    disabled  inactive  -
```

### `pihole-ftl`

- `Type=simple`, `Restart=on-failure`, `TimeoutStopSec=60s`.
- **`install-mode: disable`** — it does **not** start after installation. This is
  deliberate, so the operator can free port 53 and review plugs first. The charm
  must explicitly `snap start --enable pihole-by-rajannpatel.pihole-ftl`.
- `refresh-mode: endure` — survives `snap refresh`, so DNS does not drop during
  a refresh.
- Unit name: `snap.pihole-by-rajannpatel.pihole-ftl.service`.

### `gravity-sync`

- `Type=oneshot`, driven by `snap.pihole-by-rajannpatel.gravity-sync.timer`
  (`OnCalendar=Sun *-*-* 04:25`, from `--timer="sun,03:00~05:00"`).
- **Enabled by default.**
- **The schedule is not configurable through the snap.** The configure hook
  rejects any `timer.*` key and aborts the whole transaction
  (`meta/hooks/configure:137-192`). Changing it requires a host-side systemd
  drop-in that the charm must write itself:
  `/etc/systemd/system/snap.pihole-by-rajannpatel.gravity-sync.timer.d/override.conf`.

## Interfaces

Auto-connected on install — enough to serve DNS and the web UI with no
intervention:

`network`, `network-bind`, `network-observe`, `shared-memory` (private)

**Require manual `snap connect`:**

| Plug | Buys you |
|---|---|
| `network-control` | DHCP server mode |
| `firewall-control` | DHCP server mode |
| `system-observe` | per-process DNS attribution (`/proc/<pid>/comm`) |
| `time-control` | NTP client (`ntp.sync`, needs CAP_SYS_TIME) |
| `process-control` | `misc.nice` (needs CAP_SYS_NICE) |
| `hardware-observe` | hardware info on the diagnostics page |
| `mount-observe` | filesystem info on the diagnostics page |

Verified empirically: without `time-control`, `process-control`, and
`system-observe`, `FTL.log` emits `CAP_SYS_TIME required`, `CAP_SYS_NICE
required`, and `Could not fopen("/proc/<pid>/comm")` warnings, plus AppArmor
`DENIED` lines in `dmesg`. Connecting those three and restarting makes all of
them disappear.

`snap connect` is idempotent — reconnecting an already-connected plug is a no-op,
so it is safe to run on every reconcile:

```
snap connect pihole-by-rajannpatel:network-control
snap connect pihole-by-rajannpatel:firewall-control
snap connect pihole-by-rajannpatel:system-observe
snap connect pihole-by-rajannpatel:hardware-observe
snap connect pihole-by-rajannpatel:mount-observe
snap connect pihole-by-rajannpatel:process-control
snap connect pihole-by-rajannpatel:time-control
```

## Configuration: `snap set`

The snap proxies snapd config to `pihole-FTL --config`. Every FTL key is exposed
as `ftl.<upstream.key>`, written into `pihole.toml`, and FTL is restarted **only
if the value actually changed** (`meta/hooks/configure:263-267`).

Idempotency verified by PID: setting the same value twice does not restart.
Safe to apply on every `config-changed`.

`snap get` returns `has no configuration` until the daemon has started once;
`bin/config-sync` then syncs all 166 `pihole.toml` keys into snapd state.

### Trap 1: 66 of 166 FTL keys are unreachable

snapd validates option names with
`^(?:[a-z0-9]+-?)*[a-z](?:-?[a-z0-9])*$`
([snapd `overlord/configstate/config/helpers.go`](https://github.com/canonical/snapd/blob/master/overlord/configstate/config/helpers.go)),
which **rejects camelCase, uppercase, and underscores**. FTL v6 uses both freely.
This is a snapd limitation, not a snap bug, and there is **no workaround through
`snap set`** — the keys cannot even be *read*.

```
$ snap set pihole-by-rajannpatel ftl.dns.listeningMode=LISTEN_ALL
error: ... (invalid option name: "listeningMode")
```

Notable unreachable keys:

- **`dns.listeningMode`** — the worst one. Default is `LISTEN_LOCAL`; a Pi-hole
  serving a network needs `LISTEN_ALL`. **The charm's primary use case cannot be
  configured declaratively.**
- `dns.queryLogging`, `dns.blockTTL`, `dns.revServers`, `dns.cnameRecords`,
  `dns.hostRecord`, `dns.bogusPriv`, `dns.domainNeeded`, `dns.expandHosts`,
  `dns.rateLimit.count`, `dns.rateLimit.interval`,
  `dns.reply.host.IPv4`/`IPv6`, `dns.specialDomains.*`
- `dhcp.leaseTime`, `dhcp.rapidCommit`, `dhcp.multiDNS`,
  `dhcp.ignoreUnknownClients`
- all of `resolver.*` (`resolveIPv4`, `resolveIPv6`, `macNames`, `networkNames`,
  `refreshNames`)
- `database.maxDBdays`, `database.DBinterval`, `database.useWAL`
- `webserver.api.max_sessions`, `webserver.api.totp_secret`,
  `webserver.api.allow_destructive`, `webserver.serve_all`
- `misc.etc_dnsmasq_d`, `misc.dnsmasq_lines`, `misc.extraLogging`,
  `misc.readOnly`, `misc.delay_startup`

Reachable keys that cover the essentials:

`dns.upstreams`, `dns.port`, `dns.interface`, `dns.hosts`, `dns.domain.name`,
`dns.cache.size`, `dns.blocking.active`, `dns.blocking.mode`,
`dhcp.active`, `dhcp.start`, `dhcp.end`, `dhcp.router`, `dhcp.netmask`,
`dhcp.ipv6`, `dhcp.logging`, `dhcp.hosts`,
`webserver.port`, `webserver.domain`, `webserver.acl`, `webserver.threads`,
`webserver.api.password`, `webserver.api.pwhash`, `webserver.tls.cert`,
`webserver.session.timeout`, `ntp.*`, `misc.privacylevel`, `files.log.*`

**Design consequence.** The charm must be a hybrid: `snap set` for reachable
keys, and a fallback that invokes `pihole-FTL --config <key> <value>` directly
(via `snap run --shell`) plus a manual `snap restart` for the rest. The fallback
desynchronises snapd state from `pihole.toml`, which is unavoidable — document it
rather than hiding it.

### Trap 2: `ftl.dns.dnssec` is a silent no-op

```
$ snap set pihole-by-rajannpatel ftl.dns.dnssec=true   # exit 0, no warning
$ grep dnssec .../pihole.toml                          # dnssec = false — UNCHANGED
```

The configure hook migrates `dns.dnssec` to `dns.dnssec_enabled`
(`meta/hooks/configure:212-214`), but FTL v6.7 still reads `dns.dnssec`, and
`dnssec_enabled` is not a valid snapd option name (underscore). The value is
dropped and `snap set` reports success.

**Generalise this: the exit code of `snap set` is not evidence.** Always read
back — either `pihole.toml` or the snapd state.

### Trap 3: DHCP keys have a mandatory order

```
$ snap set ... ftl.dhcp.active=true            # with empty pool
error: ... (run hook "configure": Error applying ftl.dhcp.active=true)
        # underlying: pihole-FTL --config dhcp.active true
        #   -> "DHCP start address is not valid" (exit 3)
```

Write `dhcp.start`, `dhcp.end`, `dhcp.router` **before** `dhcp.active`. And if
the bind on port 67 fails, FTL does not degrade — it crash-loops via
`Restart=on-failure`.

**NOT VERIFIED**: whether DHCP works end-to-end under strict confinement. In
testing the bind failed with `EADDRINUSE` (LXD's dnsmasq on `lxdbr0:67`), which
is a port conflict rather than an AppArmor denial — so confinement does not
*appear* to be the blocker, but it was never proven on a host with 67 free. Test
in a dedicated VM before exposing DHCP in the charm.

### Admin password

Declarative:

```
snap set pihole-by-rajannpatel ftl.webserver.api.password='<plaintext>'
```

FTL hashes it into `webserver.api.pwhash` (`$BALLOON-SHA256$...`) and restarts.

**But the plaintext persists in snapd state** — `GET /v2/snaps/.../conf` returns
it verbatim. Anyone with snapd access can read it. Options:

1. Read the password from a Juju secret, apply it, then
   `snap unset pihole-by-rajannpatel ftl.webserver.api.password` since the hash
   is already in the TOML. **NOT VERIFIED**: whether the unset triggers a pwhash
   reset. Prove it before relying on it.
2. Use the imperative path instead:
   `pihole-by-rajannpatel.pihole setpassword '<pw>'` → `[✓] New password set`.
   Verified to rewrite `pwhash`; the new hash propagates into snapd state on the
   next daemon start.

### Blocklists / adlists are not declarative

They live in the `adlist` table of `gravity.db`, not in config. The launcher
seeds Steven Black's list on first boot with `INSERT OR IGNORE`
(`bin/launcher-ftl:67-79`). Managing them requires the v6 HTTP API
(`/api/lists`) or `pihole-FTL sqlite3` plus `pihole -g`. Neither is idempotent or
transactional — design the reconcile step defensively.

## Ports

Verified with `ss -tulpn`:

| Port | Use | Notes |
|---|---|---|
| 53 tcp+udp | DNS | `ftl.dns.port` |
| 80 tcp | admin UI + API | default `webserver.port = "80o,443os,[::]:80o,[::]:443os"`; the `o` suffix means *optional* — it does not fail if taken |
| 443 tcp | HTTPS | the `s` suffix; needs `webserver.tls.cert` |
| **123 udp** | **NTP server — active by default** | `ntp.ipv4.active` / `ntp.ipv6.active` default `true`. Unexpected attack surface. Both keys are reachable, so the charm should either open it deliberately or set them `false`. |
| 67 / 547 udp | DHCP / DHCPv6 | only when `dhcp.active=true` |
| 4711 | **not used** | that was FTL v5's telnet API. v6 serves the API over HTTP on `webserver.port`. |

## Paths

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

**`$SNAP_DATA` is versioned per revision** (`current` → `1348`). snapd copies the
tree on refresh. Never hardcode the revision number.

FTL runs **as root** inside the sandbox, not as a `pihole` user.

## Commands

The declared alias `pihole` **does not auto-register** — that needs a store
assertion this snap does not have. `snap aliases | grep pihole` is empty after
install. Use the fully qualified name `pihole-by-rajannpatel.pihole`, or run
`snap alias pihole-by-rajannpatel.pihole pihole` during install.

Apps: `pihole`, `pihole-ftl` (daemon), `snap-check`, `snap-setup`, `snap-debug`,
`sqlite3`, `gravity-sync`.

### Commands that lie

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

### v6 equivalents (verified)

| Intent | Correct v6 command |
|---|---|
| Set password | `snap set ... ftl.webserver.api.password='<pw>'` or `pihole setpassword '<pw>'` |
| Restart DNS | `snap restart pihole-by-rajannpatel.pihole-ftl` |
| Reload lists + flush cache | `pihole-by-rajannpatel.pihole reloaddns` |
| Reload lists, keep cache | `pihole-by-rajannpatel.pihole reloadlists` |
| Status | `pihole-by-rajannpatel.pihole status` |
| Update gravity | `pihole-by-rajannpatel.pihole -g` |
| Health check | `pihole-by-rajannpatel.pihole snap-check` |
| Query the API | `pihole-by-rajannpatel.pihole api <endpoint>` → JSON |

`pihole api dns/blocking` → `{"blocking":"enabled","timer":null,"took":...}` is
the best signal available for charm status.

`snap-check` has **semantic exit codes** (`bin/snap-check:39,55,92,97,141`):

- `0` — OK
- `1` — config error (a required plug is disconnected)
- `2` — runtime error (port conflict)

It checks plugs, ports 53/80/67/546, and AppArmor denials. Use it in
`collect_unit_status`.

Subcommands the launcher intercepts and rejects with exit 1
(`bin/launcher-pihole:53-62`): `-up`, `updatePihole`, `updatechecker`,
`uninstall`, `checkout`. `-r`/`repair` redirects to `snap-setup`.

Without root, only `""`, `-h`, `--help`, `help`, `-v`, `--version`, `version`,
`status`, `-q`, `query`, `snap-check` are allowed.

## systemd-resolved and port 53

**The snap cannot handle this. It is entirely the charm's job.** Strict
confinement prevents the snap from writing to `/etc/systemd/`.

```
mkdir -p /etc/systemd/resolved.conf.d
printf '[Resolve]\nDNS=127.0.0.1\nDNSStubListener=no\n' \
  > /etc/systemd/resolved.conf.d/pihole.conf
systemctl restart systemd-resolved
```

`bin/snap-check:86-92` detects the conflict (`127.0.0.53:53`) and prints exactly
this remediation with exit 2.

**And the charm must undo it on removal.** `bin/launcher-pihole:40-48` warns that
*"snap confinement cannot restore it during removal"*. Delete the drop-in in the
`remove` hook and restart `systemd-resolved`.

The launcher **no longer pre-flight-checks port 53** (`bin/launcher-ftl:20-24`):
*"If port 53 is occupied, FTL will log a clear EADDRINUSE error and crash."*
Combined with `Restart=on-failure`, that is an indefinite crash loop. **Check the
port before starting** — `snap-check` returning 2 is the cheapest way.

## Readiness is not the same as active

`bin/launcher-ftl:67-121`: on first boot, if `gravity.db` is missing, the
launcher runs `pihole -g` synchronously to create the schema, inserts the default
adlist, then **forks a background child** that waits up to 90s for FTL to answer
DNS (`dig @127.0.0.1 . NS`) before downloading the list.

So `pihole-ftl` is `active` long before blocking works. The charm must not go
`active/idle` on `snap services` output. Gate on `pihole api dns/blocking`
responding, and optionally on `gravity.db` exceeding a sane size.

## Required install sequence

1. Free port 53 (systemd-resolved drop-in).
2. `snap install pihole-by-rajannpatel`.
3. `snap connect` the manual plugs that apply.
4. `snap alias`, or commit to the fully qualified command name.
5. Apply config: `snap set ftl.*` for reachable keys, `pihole-FTL --config` for
   the rest.
6. `snap start --enable pihole-by-rajannpatel.pihole-ftl`.
7. Poll readiness via the HTTP API, not via systemd.

## Known snap bug worth remembering

`bin/snap-check:106` suggests `snap set <snap> webserver.port=8080` — **missing
the `ftl.` prefix**. The configure hook only reads the `ftl` and `timer`
namespaces (`configure:137,223`), so that command is accepted into snapd state
and does nothing. Always use `ftl.webserver.port`.
*(Verified by code inspection, not at runtime.)*
