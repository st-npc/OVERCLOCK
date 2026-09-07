# Overclock

Peer-to-peer resource sharing for low-end devices: when a laptop is
overloaded, it offloads a slice of a splittable job to nearby idle devices
instead of thrashing to disk, and merges the results back. If no helper is
reachable, or one drops mid-run, the job still completes correctly using
only what's available.

## Running it

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

Start the orchestrator + dashboard (on macOS, port 5000 is often taken by
AirPlay Receiver — use another port if `app_main.py` fails to bind):

```bash
python app_main.py --port 5050
```

Open `http://localhost:5050`, add each helper's `host:port` under
**Helper devices**, set a batch size, and click **Start** — this both
connects to the helpers and kicks off the run in one action. Leave the
helper list empty to run fully local.

### Simulating multiple devices on one machine

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
| `task.py` | The demo workload itself (image batch transform). No networking/Flask imports — swap this file to change the demo task. |
| `net_client.py` | All outbound HTTP to helpers: timeouts, response validation, typed errors (`HelperUnreachable`, `HelperBadResponse`). |
| `device_monitor.py` | Local + helper stats polling, rolling ~60s history for sparklines, status state machine (`idle`/`busy`/`unreachable`/`reconnecting`), spare-capacity scoring. |
| `orchestrator.py` | Job lifecycle: reachability probe → proportional split → concurrent dispatch → per-chunk fallback on failure → ordered merge → `processed_output/`. |
| `interfaces.py` | Best-effort local network interface detection (used to surface a USB-C link in the dashboard). |
| `app_helper.py` | Helper Flask service: `GET /stats`, `POST /process`. |
| `app_main.py` | Orchestrator Flask service: dashboard, `POST /api/start`, `GET /api/events` (SSE), `GET /api/status`, `GET /api/interfaces`. |
| `templates/`, `static/` | Dashboard UI. Hand-rolled canvas sparklines, no CDN dependency — works fully offline on a bare LAN. |

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

## What's solid vs. stubbed

See the final status summary from the build session for the current,
honest breakdown of what's demo-ready versus future scope.
