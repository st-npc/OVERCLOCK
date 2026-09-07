"""Best-effort local network interface detection, used to surface a
USB-C (RNDIS/NCM) link as a selectable option in the dashboard.

This can only detect an interface that the OS has already brought up —
enabling USB tethering/networking itself is an OS-level step the user has
to do by hand (see README.md). Nothing here touches OS network settings.
"""
import re

import psutil

_USB_NAME_HINTS = re.compile(r"(usb|rndis|ncm|ecm)", re.IGNORECASE)
_LOOPBACK_HINTS = re.compile(r"^(lo|loopback)", re.IGNORECASE)


def list_network_interfaces() -> list:
    """Return every up, non-loopback interface with an IPv4 address, each
    flagged with a best-effort guess of whether it's a USB network link.

    The name-based heuristic is deliberately visible in the API response
    (`likely_usb`) rather than hidden — it can misfire on unusual adapter
    names, and the dashboard should let the user judge for themselves.
    """
    results = []
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
    except Exception:
        return results

    for name, addr_list in addrs.items():
        if _LOOPBACK_HINTS.match(name):
            continue
        iface_stats = stats.get(name)
        if iface_stats is not None and not iface_stats.isup:
            continue

        ipv4 = next((a.address for a in addr_list if a.family.name == "AF_INET"), None)
        if not ipv4:
            continue

        results.append(
            {
                "name": name,
                "ip": ipv4,
                "likely_usb": bool(_USB_NAME_HINTS.search(name)),
            }
        )

    results.sort(key=lambda r: (not r["likely_usb"], r["name"]))
    return results
