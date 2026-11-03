"""mDNS discovery of Miracast over Infrastructure (MS-MICE) sinks.

MS-MICE section 3.1.1 (Initialization) requires a Sink to register the
service instance name ``<friendly name>._display._tcp.local`` with its
mDNS responder, carrying an SRV record for TCP port 7250 and a TXT
record ``container_id=<GUID>``. Browsing that service type finds every
infrastructure-capable Sink on the network with no Wi-Fi Direct
involvement at all, which is the point of the transport.

python-zeroconf is imported lazily and only here: the codec and TCP
client in mice.py are stdlib-only, and a host without zeroconf can
still use every other FluxCast mode. Callers receive ``WFDNotReady``
when the library is missing so session code can present the fix rather
than a traceback.

Deployment note: python-zeroconf speaks raw multicast (224.0.0.251 and
ff02::fb, UDP 5353) from userspace, so it works even where the system
resolver has mDNS disabled - but a host-side firewall that drops
non-RFC1918 output will silence discovery entirely; see the design
document's firewall section before blaming the network.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import WFDNotReady

MICE_SERVICE_TYPE = "_display._tcp.local."
MICE_DEFAULT_BROWSE_TIMEOUT = 5.0
_RESOLVE_TIMEOUT_MS = 2500


@dataclass(frozen=True)
class MiceSink:
    """One discovered MS-MICE sink (the resolved DNS-SD service)."""

    name: str                     # friendly name from the instance label
    addresses: tuple[str, ...]    # resolved addresses, v4 then v6
    port: int                     # SRV port; 7250 per the spec
    container_id: Optional[str]   # TXT container_id GUID
    server: Optional[str] = None  # SRV target host, when advertised

    @property
    def host(self) -> str:
        """Best address for mice_connect(): a literal beats .local names,
        which need an mDNS-aware resolver the OS may not have."""
        if self.addresses:
            return self.addresses[0]
        return self.server or ""


def _txt_value(properties: dict, key: str) -> Optional[str]:
    """TXT properties may key and value as bytes or str depending on the
    zeroconf version; normalise before lookup."""
    for k, v in properties.items():
        k_text = k.decode("utf-8", "replace") if isinstance(k, bytes) else k
        if k_text != key:
            continue
        if v is None or isinstance(v, str):
            return v
        if isinstance(v, bytes):
            return v.decode("utf-8", "replace")
        return str(v)
    return None


def _parsed_addresses(info: Any) -> tuple[str, ...]:
    """All addresses from a resolved ServiceInfo, v4 and v6 where present.

    parsed_addresses() covers both families on modern zeroconf; fall back
    to decoding the raw IPv4 bytes for older versions.
    """
    parsed = getattr(info, "parsed_addresses", None)
    if callable(parsed):
        try:
            addresses = list(parsed())
        except Exception:
            addresses = []
        if addresses:
            return tuple(addresses)
    raw = getattr(info, "addresses", None) or []
    return tuple(socket.inet_ntoa(a) for a in raw)


def _friendly_name(service_name: str, service_type: str) -> str:
    """Strip the DNS-SD suffix: 'Room 3._display._tcp.local.' -> 'Room 3'."""
    suffix = "." + service_type
    if service_name.endswith(suffix):
        return service_name[: -len(suffix)]
    return service_name


def _sink_from_info(service_name: str, service_type: str, info: Any) -> MiceSink:
    return MiceSink(
        name=_friendly_name(service_name, service_type),
        addresses=_parsed_addresses(info),
        port=info.port,
        container_id=_txt_value(info.properties, "container_id"),
        server=getattr(info, "server", None),
    )


class _SinkCollector:
    """Zeroconf listener collecting one MiceSink per service, keyed by
    container_id (falling back to the instance name) so duplicate
    announcements collapse instead of duplicating entries.

    Resolution happens in the callbacks, which run on zeroconf's engine
    threads; the lock keeps the dict consistent with the browse thread.
    """

    def __init__(self, service_type: str) -> None:
        self._service_type = service_type
        self._lock = threading.Lock()
        self._sinks: dict[str, MiceSink] = {}

    def _key(self, sink: MiceSink) -> str:
        return sink.container_id or sink.name

    def _resolve(self, zeroconf: Any, service_type: str, name: str) -> None:
        try:
            info = zeroconf.get_service_info(
                service_type, name, timeout=_RESOLVE_TIMEOUT_MS)
        except Exception:
            return
        if info is None or not info.port:
            return
        sink = _sink_from_info(name, service_type, info)
        with self._lock:
            self._sinks[self._key(sink)] = sink

    def add_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        if service_type == self._service_type:
            self._resolve(zeroconf, service_type, name)

    def update_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        if service_type == self._service_type:
            self._resolve(zeroconf, service_type, name)

    def remove_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        if service_type != self._service_type:
            return
        friendly = _friendly_name(name, service_type)
        with self._lock:
            for key, sink in list(self._sinks.items()):
                if sink.name == friendly:
                    del self._sinks[key]

    def results(self) -> list[MiceSink]:
        with self._lock:
            return sorted(self._sinks.values(), key=lambda s: s.name)


def browse_mice_sinks(
    timeout: float = MICE_DEFAULT_BROWSE_TIMEOUT,
    service_type: str = MICE_SERVICE_TYPE,
) -> list[MiceSink]:
    """Browse the network for MS-MICE sinks; empty list means none found.

    Callers decide policy on an empty result: per the design document the
    session layer falls back to standard WFD in that case. Raises
    WFDNotReady only when python-zeroconf itself is unavailable.
    """
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError as exc:
        raise WFDNotReady(
            "python-zeroconf is required for MS-MICE discovery "
            "(pip install zeroconf)"
        ) from exc

    collector = _SinkCollector(service_type)
    zeroconf = Zeroconf()
    try:
        ServiceBrowser(zeroconf, service_type, listener=collector)
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        zeroconf.close()
    return collector.results()
