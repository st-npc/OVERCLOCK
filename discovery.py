"""Zero-dependency LAN auto-discovery for helper devices.

Every helper address today has to be typed in by hand as `host:port` —
fine for a demo, tedious for a real LAN with several idle machines. This
module lets a helper announce itself over a UDP broadcast and the main
device collect replies, so the dashboard can offer a "Discover helpers on
LAN" button instead of a manual IP hunt.

Kept dependency-free on purpose (stdlib `socket` only), matching the rest
of this project's zero-extra-deps philosophy (see security.py's docstring)
— pulling in zeroconf/mDNS for a LAN demo tool would be a heavier
dependency footprint than a two-packet UDP handshake needs.

Protocol (fits in one UDP datagram each way, no fragmentation risk):
  main -> broadcast: {"magic": "OVERCLOCK_DISCOVER_V1"}
  helper -> unicast reply to sender: {"magic": "OVERCLOCK_HELPER_V1",
                                       "port": <int>,
                                       "passphrase_required": <bool>}

Never trusts the network: any malformed/foreign UDP packet on this port is
silently ignored rather than raising, same policy as every other network
boundary in this codebase (see app_helper.py's docstring).
"""
import json
import socket
import threading
import time

import psutil

DISCOVERY_PORT = 50505  # arbitrary high port, chosen to avoid common collisions
MAGIC_REQUEST = "OVERCLOCK_DISCOVER_V1"
MAGIC_REPLY = "OVERCLOCK_HELPER_V1"
MAX_PACKET_BYTES = 512  # generous for this tiny JSON payload; caps a malformed/oversized packet cheaply
DEFAULT_TIMEOUT = 1.5


def _safe_json_loads(raw: bytes):
    try:
        if len(raw) > MAX_PACKET_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class DiscoveryResponder:
    """Runs on a helper: listens for broadcast discovery requests and
    replies with this helper's real HTTP port. Started as a daemon thread
    from app_helper.py; harmless to leave running for the process lifetime
    since it only ever answers a well-formed discovery request."""

    def __init__(self, helper_port: int, passphrase_required: bool, discovery_port: int = DISCOVERY_PORT):
        self.helper_port = helper_port
        self.passphrase_required = passphrase_required
        self.discovery_port = discovery_port
        self._sock = None
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass  # not fatal — SO_REUSEADDR alone is enough on most platforms
        try:
            sock.bind(("", self.discovery_port))
        except OSError as exc:
            print(f"Discovery: could not bind UDP port {self.discovery_port} ({exc}) — auto-discovery replies disabled")
            sock.close()
            return
        sock.settimeout(0.5)  # so the loop can notice _stop_event without blocking forever
        self._sock = sock
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._sock:
            self._sock.close()

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                raw, addr = self._sock.recvfrom(MAX_PACKET_BYTES)
            except socket.timeout:
                continue
            except OSError:
                break  # socket closed (stop() called) or a transient network error

            data = _safe_json_loads(raw)
            if not data or data.get("magic") != MAGIC_REQUEST:
                continue

            reply = {
                "magic": MAGIC_REPLY,
                "port": self.helper_port,
                "passphrase_required": self.passphrase_required,
            }
            try:
                self._sock.sendto(json.dumps(reply).encode("utf-8"), addr)
            except OSError:
                pass  # best-effort — a dropped reply just means that helper isn't discovered this round


def _directed_broadcast_addresses() -> list:
    """Every up, non-loopback IPv4 interface's directed broadcast address
    (e.g. 192.168.1.255), computed from its address + netmask. Some
    routers/OS network stacks drop the limited broadcast 255.255.255.255
    but still deliver a directed broadcast on the local subnet, so
    discover_helpers() tries both rather than relying on just one."""
    targets = []
    try:
        addrs = psutil.net_if_addrs()
    except Exception:
        return targets
    for name, addr_list in addrs.items():
        if name.lower().startswith(("lo", "loopback")):
            continue
        for a in addr_list:
            if a.family.name != "AF_INET" or not a.netmask:
                continue
            try:
                ip_bits = [int(o) for o in a.address.split(".")]
                mask_bits = [int(o) for o in a.netmask.split(".")]
                bcast = [ip_bits[i] | (255 - mask_bits[i]) for i in range(4)]
                targets.append(".".join(str(b) for b in bcast))
            except (ValueError, IndexError):
                continue
    return targets


def discover_helpers(timeout: float = DEFAULT_TIMEOUT, discovery_port: int = DISCOVERY_PORT) -> list:
    """Broadcast a discovery request and collect replies for `timeout`
    seconds. Returns a list of {"address": "host:port", "passphrase_required":
    bool} dicts, one per distinct responding IP (if a helper somehow replies
    twice, only the first reply is kept)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.2)

    request = json.dumps({"magic": MAGIC_REQUEST}).encode("utf-8")
    targets = ["255.255.255.255"] + _directed_broadcast_addresses()
    sent_at_least_once = False
    for target in targets:
        try:
            sock.sendto(request, (target, discovery_port))
            sent_at_least_once = True
        except OSError:
            continue  # this particular target is unreachable/blocked — try the others

    if not sent_at_least_once:
        sock.close()
        return []  # no network interface could send a broadcast at all — nothing to discover

    found = {}
    end_at = time.monotonic() + timeout
    while time.monotonic() < end_at:
        try:
            raw, addr = sock.recvfrom(MAX_PACKET_BYTES)
        except socket.timeout:
            continue
        except OSError:
            break

        data = _safe_json_loads(raw)
        if not data or data.get("magic") != MAGIC_REPLY:
            continue
        port = data.get("port")
        if not isinstance(port, int) or not (0 < port < 65536):
            continue

        ip = addr[0]
        if ip not in found:
            found[ip] = {
                "address": f"{ip}:{port}",
                "passphrase_required": bool(data.get("passphrase_required")),
            }

    sock.close()
    return sorted(found.values(), key=lambda d: d["address"])
