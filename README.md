# Overclock

Peer-to-peer resource sharing for low-end devices: when a laptop is
overloaded, it offloads a slice of a splittable job to nearby idle devices
instead of thrashing to disk, and merges the results back. If no helper is
reachable, or one drops mid-run, the job still completes correctly using
only what's available.

## Running it

**Easiest way**: one command handles the venv (first run only), starts the
orchestrator plus 2 local helpers (so you immediately see multi-device
behavior), and opens the dashboard in your browser. Ctrl+C stops
everything.

```bash
./run.sh          # main + 2 local helpers
./run.sh 0        # main only, fully local
./run.sh 4        # main + 4 local helpers
```

On macOS you can instead just **double-click `Overclock.command`** in
Finder — no terminal typing at all (it still opens a terminal window to
run in, since these are long-running local servers, but you don't have to
type anything into it). On Windows, `run.bat` does the same thing (not
independently tested on real Windows — if it misbehaves, fall back to the
manual steps below and let me know what broke).

Set `OVERCLOCK_MAIN_PORT`/`OVERCLOCK_HELPER_BASE_PORT` env vars to change
the default ports (5050 / 5001) — useful on macOS where port 5000 itself
is usually taken by AirPlay Receiver.

<details>
<summary>Manual steps (what run.sh does, if you want to run pieces yourself)</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start a helper on any machine (or a second terminal on the same machine,
on a different port, to simulate a second device):

```bash
python app_helper.py --port 5001
```

Start the orchestrator + dashboard:

```bash
python app_main.py --port 5050
```

</details>

Open `http://localhost:5050`, add each helper's `host:port` under
**Helper devices**, set a batch size, and click **Start** — this both
connects to the helpers and kicks off the run in one action. Leave the
helper list empty to run fully local.

Each helper also serves its own page at `http://<its-address>:<port>/` —
the **Receiver** view. Open it on the helper machine itself to see that
device's own live stats and a **Pause Receiving** / **Start Receiving**
toggle. Pausing doesn't stop the process — it just tells the orchestrator's
next job "don't send me work"; anything already in flight still finishes.
A paused helper shows up on the main dashboard as `PAUSED`, is excluded
from the split, and the log says so explicitly.

The main dashboard also has a **"Who's doing the work"** panel: a live bar
showing exactly what fraction of the current batch went to the local
device versus each helper, filling in per-device as chunks complete.

### First-run intro: "break the screen"

The first time you open the dashboard, a full-screen three.js scene covers
it: a bright, RGB-lit stylized PC rendered from primitives (no external 3D
model files) — a hue-cycling glow chases around the monitor's rim and the
tower's LED strip against a vivid purple-to-teal backdrop. The monitor
plays a short looping coding video. Tap/click the screen a handful of
times and it cracks a little more each time (right on top of the playing
video), then shatters — the fragments fly apart with basic gravity,
freeze-framed mid-video — revealing the real dashboard underneath. There's
a low-key **Skip** link if you'd rather not. It only plays once per
browser (a `localStorage` flag remembers you've seen it); clear that key
or open a private window to replay it. It's fully self-contained, gated
behind `prefers-reduced-motion` (skips straight to the dashboard if that's
set), and tears down its own render loop, pauses/unloads the video, and
frees its GPU resources the moment it's dismissed — it doesn't linger and
burn CPU/GPU after you're past it, which would be a bad look for a tool
whose whole point is honest resource accounting. Source: `static/intro.js`.

The coding video (`static/media/coding.mp4`, ~5.3MB) is CC0 stock footage
from Pexels ("Typing of codes", via Coverr) — free to use, no attribution
required — downloaded once and vendored locally rather than streamed from
anywhere at runtime, same offline-safety reasoning as three.js below.

The dashboard itself also has two more subtle three.js touches:
- A faint, slow-drifting node field in the background (`static/bg.js`) —
  low particle count, low opacity, pauses itself when the tab isn't
  visible and skips entirely under `prefers-reduced-motion`.
- A bold **"Spare capacity available right now"** headline number at the
  top, averaged live across every reachable device and animated (eased
  count-up) whenever it changes — a single at-a-glance number for "how
  much idle power is on this network right now."

All of this runs off three.js **vendored locally** in `static/vendor/`
(not pulled from a CDN at runtime), so the offline-on-a-bare-LAN demo
guarantee from the rest of this README still holds.

### Demoing the adaptive offload: "Simulate local overload"

The amber panel at the top of the dashboard (`stress.py`) generates real
CPU/RAM load on your local device — actual busy OS processes, not a fake
flag — so you can demo the core "struggling laptop" story on demand
instead of needing your machine to genuinely be busy:

1. Set CPU workers / RAM (MB) and click **Start Overload**. Watch *This
   device*'s card: CPU%, RAM%, and spare capacity all react for real,
   same as any other load would.
2. With one or more helpers configured, click the real **Start** button.
   Because the split (`orchestrator._compute_shares`) already reduces the
   local share as local load rises, you'll see the batch shift hard
   toward the helper — the "Who's doing the work" panel and the helper's
   own card show it visibly taking on the work your overloaded device
   couldn't.
3. **Stop Overload** to release the CPU workers and freed RAM immediately
   and watch the local card recover.

This is a demo/testing tool, not part of the sharing pipeline — it works
purely by making the local machine genuinely busier, so orchestrator.py
and device_monitor.py need zero awareness of it. Workers run as daemon
OS processes (not threads, so they use real cores instead of being
capped by the GIL) and are capped (CPU workers ≤ core count, RAM stress
never eats past a 512MB floor of free memory) so it can't actually crash
your machine; they're also killed automatically if `app_main.py` itself
exits, even uncleanly.

### Simulating multiple devices on one machine

`./run.sh` already does this (2 local helpers by default) — the manual
version, if you want more control over the count or ports:

```bash
python app_helper.py --port 5001
python app_helper.py --port 5002
python app_main.py --port 5050
```

Then enter `127.0.0.1:5001` and `127.0.0.1:5002` as helpers.

### Running the fallback checkpoint tests

```bash
python tests/test_fallback.py
```

Covers: zero helpers, a helper that's never reachable, a helper that dies
after the initial reachability check but before finishing its chunk, and a
helper that returns a malformed response. All four must complete with
every image accounted for.

## Architecture

| File | Responsibility |
|---|---|
| `tasks/` | Pluggable demo workloads — see below. No networking/Flask imports in any task module. |
| `net_client.py` | All outbound HTTP to helpers: timeouts, response validation, typed errors (`HelperUnreachable`, `HelperBadResponse`). |
| `device_monitor.py` | Local + helper stats polling, rolling ~60s history for sparklines, status state machine (`idle`/`busy`/`unreachable`/`reconnecting`), spare-capacity scoring. |
| `orchestrator.py` | Job lifecycle: reachability probe → proportional split → concurrent dispatch → per-chunk fallback on failure → ordered merge → `processed_output/`. |
| `interfaces.py` | Best-effort local network interface detection (used to surface a USB-C link in the dashboard). |
| `stress.py` | Demo-only local CPU/RAM load generator (see "Simulate local overload" below). Not used by the sharing pipeline. |
| `wire.py` | zlib+JSON payload compression shared by every transport. |
| `relay_client.py` / `relay_server.py` | Cross-network relay tunnel — see below. `relay_server.py` is standalone and self-hostable. |
| `app_helper.py` | Helper Flask service: `GET /stats`, `POST /process`, optional relay-polling thread. |
| `app_main.py` | Orchestrator Flask service: dashboard, `POST /api/start`, `GET /api/events` (SSE), `GET /api/status`, `GET /api/interfaces`. |
| `templates/`, `static/` | Dashboard UI. Hand-rolled canvas sparklines; three.js is vendored locally in `static/vendor/` (not a CDN) — works fully offline on a bare LAN. |
| `static/intro.js` | One-time "break the screen" three.js intro gate — see below. Not on the sharing/job code path. |
| `static/media/` | Vendored CC0 stock video used by the intro (see below). |
| `static/bg.js` | Ambient background node-field animation (three.js). Purely decorative. |

## Pluggable tasks (not limited to images)

Sharing isn't tied to image processing — `tasks/` is a small plugin
system. Every task module implements the same four-function contract
(documented in `tasks/__init__.py`): `DISPLAY_NAME`, `generate_work(n)`,
`process_batch(items)`, `save_result(item, out_path_base)`. A work item is
just an opaque string (base64, JSON, whatever the task needs) — nothing
in the networking, orchestration, or dashboard layers knows or cares what
it actually contains.

Two tasks ship today, selectable from the **Task** dropdown on the
dashboard (also settable via `POST /api/start`'s `task_type` field, and
listed at `GET /api/tasks`):

- **`image`** — the original demo: a Pillow blur/edge-detect filter chain.
- **`render`** — Mandelbrot-set tile rendering. Genuinely CPU-heavy (pure
  per-pixel escape-time computation, no vectorization), and each work
  item is a JSON description of a viewport strip, *not* an image — proof
  the split/dispatch/fallback machinery never assumed images in the first
  place. This is the "rendering" workload: a real, splittable,
  resource-heavy job in the same spirit as compiling or video rendering.

**Adding another task** (a compile job, an ML batch, etc.) means writing
one new file under `tasks/` with those four pieces and adding one line to
`TASKS` in `tasks/__init__.py` — nothing in `app_main.py`, `app_helper.py`,
`orchestrator.py`, or the dashboard needs to change.

## Real multi-machine testing (Wi-Fi/LAN)

Everything up to now has been validated with one machine simulating
multiple devices via ports on `127.0.0.1`. That proves the logic but not
real networking — loopback has no latency, no packet loss, and no
firewall. This is the part I flagged that needs your hardware.

### Setup

1. Put both machines on the **same Wi-Fi network/router** (a phone
   hotspot works too). Some public/guest networks enable **client
   isolation**, which silently blocks device-to-device traffic even
   though both are "connected" — if reachability fails for no obvious
   reason, this is the first thing to rule out (try a home network or a
   personal hotspot instead).
2. On the **helper** machine: clone/copy this folder, create its own venv,
   `pip install -r requirements.txt` (dependencies install per machine).
3. Find the helper machine's LAN IP:
   - macOS: **System Settings → Wi-Fi → Details** (or `ipconfig getifaddr en0`)
   - Windows: `ipconfig` → "IPv4 Address" under the active adapter
   - Linux: `ip addr show` or `hostname -I`
4. On the helper machine, start the helper (it already binds to
   `0.0.0.0` by default, so it's reachable on the LAN, not just locally):
   ```bash
   python app_helper.py --port 5001
   ```
5. **Firewall prompt**: the first time, macOS/Windows will likely ask
   "Allow incoming network connections for Python?" — you must click
   **Allow**, or the helper will be unreachable from the other machine
   even though it works fine via `localhost` on its own machine.

### Verify reachability before touching the dashboard

From the **main** machine, replacing the IP with the helper's actual one:
```bash
curl http://192.168.1.42:5001/stats
```
**Correct result**: a JSON blob with `cpu_percent`/`ram_percent`/etc. that
roughly matches what that machine's own Activity Monitor/Task Manager
shows right now. If this hangs or times out, the dashboard will show the
same thing (`unreachable`) — fix reachability at this layer first, it's
much faster to debug with curl than through the UI.

### Run it for real

On the main machine: `python app_main.py --port 5050`, open
`http://localhost:5050`, enter the helper's `<lan-ip>:5001`, click Start.

**What a correct result looks like:**
- The helper's card shows live CPU/RAM that changes when you actually load
  that machine (e.g. run anything CPU-heavy on it and watch the card's
  CPU% rise and its spare-capacity bar drop within ~1-2s).
- The split in the log (`split -> local=X, <ip>=Y`) should shift toward
  the helper when the helper is idle and toward local when the helper is
  busy — try it both ways.
- `processed_output/<job_id>/` on the **main** machine ends up with every
  image, sourced from both devices.

**Real fallback test**: start a larger batch (60+), then on the helper
machine either `Ctrl+C` the helper process or turn off its Wi-Fi
mid-run. Over a real network this can surface as a hang-then-timeout
(a few seconds) rather than the instant "connection refused" you see on
loopback — that's expected and still correctly handled (`net_client`'s
timeouts bound how long it waits before falling back), just slower to
observe than the local test.

## USB-C connectivity (Priority 2)

The dashboard's **This device is reachable at** panel lists active network
interfaces and flags any whose name suggests a USB link (RNDIS/NCM/ECM).
This is detection only — **enabling USB networking between two machines is
an OS-level step that has to be done by hand first**; nothing here can
configure OS network settings.

Once enabled, the two machines get IP addresses on a private link (usually
`169.254.x.x` or a `192.168.x.x` you assign), and you use that address as
the helper's `host:port` exactly like a Wi-Fi/LAN address — the app itself
doesn't care which physical link it's going over.

### Windows
1. Plug in the USB-C cable (or a USB-C-to-USB-C / Thunderbolt cable
   between two PCs, or an RNDIS-capable phone/adapter).
2. Windows should install an "RNDIS/Remote NDIS" or "USB Ethernet"
   network adapter automatically. Check **Settings → Network & Internet →
   Ethernet** for a new connection.
3. If it doesn't appear, install the device's RNDIS driver (varies by
   cable/vendor) via Device Manager.
4. Assign a static IP on both ends (e.g. `192.168.55.1` / `192.168.55.2`,
   subnet `255.255.255.0`) via the adapter's IPv4 properties, or let both
   sides use link-local `169.254.x.x` (Windows assigns this automatically
   with no DHCP server present — just read the assigned address).

### macOS
1. Plug in a USB-C/Thunderbolt cable between two Macs, or a USB Ethernet
   adapter.
2. **System Settings → Network** — a new interface (often named
   "Thunderbolt Bridge" or "USB 10/100/1000 LAN") should appear once
   connected.
3. If connecting two Macs directly via Thunderbolt/USB-C, macOS assigns
   link-local (`169.254.x.x`) addresses automatically to both ends — no
   extra config needed. Read the address from **Network settings** or
   from Overclock's own interface panel.
4. For a USB-C-to-Ethernet dongle, it behaves like any wired NIC — connect
   it to the same LAN/switch as the other device instead of a direct link.

### Linux
1. Plug in the cable/adapter. Check `ip link` for a new interface
   (commonly `usb0`, `enx<mac>`, or `eth1`).
2. If the device doesn't bring the interface up automatically:
   `sudo ip link set usb0 up`
3. Assign an address: either let NetworkManager/DHCP handle it if the
   other end offers DHCP, or set one manually:
   `sudo ip addr add 192.168.55.1/24 dev usb0` (use `.2` on the other
   machine).
4. Some phones need "USB tethering" or "RNDIS mode" turned on in their own
   settings before Linux sees the interface at all.

**I can't verify any of this myself** — it depends on real hardware and OS
network state. What to check on your end: after enabling the link, run
`python -c "from interfaces import list_network_interfaces as f; print(f())"`
on the Overclock machine and confirm the new interface shows up (it'll be
flagged `likely_usb: True` if its name matches `usb`/`rndis`/`ncm`/`ecm`
— if your OS names it something else, like macOS's `bridge0`/`en5`, it'll
still be listed, just without the badge; use its IP either way). Then use
that IP as the helper's address on the other device's dashboard.

## Security: shared-passphrase auth

By default the network is open — anyone who can reach a helper's port can
send it work. To lock it down, start a helper with `--passphrase`:

```bash
python app_helper.py --port 5001 --passphrase hunter2
```

and enter the same passphrase in the dashboard's **Shared passphrase**
field before clicking Start — it's sent with every job to every helper (one
shared secret for the whole mesh, not per-helper credentials). A helper
missing/wrong passphrase returns 401 and is treated exactly like an
unreachable helper: the log says so and that chunk falls back to local.
`/stats` itself stays unauthenticated (read-only telemetry, and the
Receiver page's own UI depends on it working without a header) — only
`/process`, the endpoint that actually consumes CPU/RAM, is gated.

This is a shared-secret check, not encryption — traffic is still plain
HTTP. Good enough to keep casual/accidental use off your LAN; not a
substitute for a VPN if you're on a network you don't trust at all.

## Compression

Every `/process` request and response is zlib-compressed on the wire
(`wire.py`), with a fallback to plain JSON if a peer ever sends
uncompressed (so a raw `curl` against a helper still works during
debugging). The dashboard's **Compression** stat is the real measured
ratio for the run, not an estimate — the render task's JSON work
descriptions compress especially well; image payloads still shrink
meaningfully since compressing the outer request/response envelope
recovers some of base64's overhead.

## Session summary

Every completed run reports, in the summary strip: total wall time, an
estimate of how long fully-local processing would have taken (from the
local device's own measured per-item rate this run), and the resulting
time saved. The "Who's doing the work" panel's legend gets a per-device
throughput figure (img/s) once each device finishes. If a run ends up
fully local (no helpers, or all unreachable), the estimate naturally
converges to the actual time and shows "no faster."

## Seeing the load actually move during a real run

By default the dashboard's device stats only refresh once a second, and
the demo "image" task processes a 24-item batch in well under a second —
too fast to visibly register on a 1s-cadence graph before it's over. Two
things fix this so a real job's effect on your CPU/RAM is actually
visible, not just a claim in the log:

- **Fast polling during a job.** The moment a job starts, both the
  internal device monitor and the dashboard's live event stream switch
  from a 1s to a 0.2s refresh cadence (reverting the instant the job
  ends), so a short burst of local or helper load gets sampled instead of
  falling between two 1-second-apart snapshots.
- **Explicit before/peak numbers, not just a sparkline to eyeball.** Every
  device that does work in a run is tracked from the moment it's assigned
  work: its real CPU%/RAM% right before, and the real peak measured while
  it was processing. Once the run finishes, the "Who's doing the work"
  panel shows this under each device's item count and throughput, e.g.
  `CPU 12%→64% · RAM 41%→45%` — a concrete, measured before/after for
  local *and* every helper, not an estimate.

For the effect to be visually obvious on the sparklines too (not just the
before/peak numbers), give the job enough real work to take at least a
second or two: bump **Batch size** well above the default 24 (100-200 is
plenty), or switch **Task** to the fractal renderer, which is deliberately
heavier per item (~2s for 24 tiles) than the synthetic image filter. For a
dramatic, on-demand demonstration of the same reactive split — without
waiting on a real batch — use **Simulate local overload** above the
controls: it loads this device with genuine CPU/RAM work you can watch
build up, then when you start a job while it's active you can see the
split shift hard toward the helper, and recover the instant you stop it.

## Cross-network relay (Priority 3 — needs infrastructure you host)

Direct connections need both devices reachable from each other (same
LAN/USB link). For two devices on **different networks** — different
Wi-Fi, different NAT — Overclock can tunnel through a relay server both
sides can reach instead, the same shape as a TURN relay for video calls.

**I cannot host this for you.** `relay_server.py` is a small, dependency-free,
standalone Flask app — deploy it yourself somewhere both devices can reach:
a $5 VPS, a free tier on Render/Fly/Railway, or even a third machine on a
network both devices can reach. Nothing else in this repo needs to go
with it; it's one file.

```bash
# on the relay host, port open to both devices:
python relay_server.py --port 5090
```

On the device that will lend capacity, start its helper in relay mode
instead of (or alongside) direct listening:

```bash
python app_helper.py --port 5001 --relay http://your-relay-host:5090 --room demo123 --device-id laptop-b
```

`--room` is any shared code you make up — both devices just need to agree
on it (treat it like a meeting code, not a secret; combine with
`--passphrase` if you want the traffic itself authenticated). On the
**main** device's dashboard, add this as the helper address instead of a
`host:port`:

```
relay://your-relay-host:5090/demo123/laptop-b
```

Everything else — the split, the fallback guarantee, compression, auth —
behaves identically over a relay connection; `net_client.py` only branches
on transport, orchestrator and the dashboard don't know the difference.
Verified locally (relay, both a direct-mode and relay-mode helper, and the
main device all running loopback, including killing a relay-connected
helper mid-job and confirming fallback still completes correctly and
detects the drop in a few seconds, not tens of seconds) — **but not across
two genuinely different networks**, which needs your own relay deployment
and two real internet connections to test honestly. What to check: after
deploying, run the two commands above with your relay's real address, and
confirm `curl http://your-relay-host:5090/relay/demo123/devices` lists
your helper's device-id before you try it from the dashboard.

## What's solid vs. stubbed

Everything in this README has been built and tested — including every
Priority 3 item (task picker, passphrase auth, compression, session
summary, cross-network relay). The one thing genuinely outside what I can
verify myself: the relay tunnel is proven correct end-to-end on loopback,
but real cross-network behavior (actual latency, NAT behavior, a relay
host you've actually deployed) needs your own testing per the steps
above. Likewise, real multi-machine Wi-Fi/LAN and any USB-C link still
need your hardware, as noted in their sections above.
