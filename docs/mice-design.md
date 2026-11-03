# MS-MICE Support — Design

Implements Miracast over Infrastructure ([MS-MICE] v6.0, published
2024-04-23) as a connection transport for FluxCast, replacing Wi-Fi
Direct with a LAN TCP connection when both Source and Sink are on the
same network. Tracks upstream feature request #61.

## Why

Wi-Fi Direct concurrency with an infrastructure association is
timing-sensitive on a single radio: GO negotiation windows can be missed
while the driver time-slices channels, and repeated failed attempts wedge
the supplicant P2P state and sometimes the sink adapter. MS-MICE removes
the radio from the equation entirely — the session runs over the wired or
Wi-Fi infrastructure the machines already share. This is the same path
Windows uses automatically when both devices are on one network.

## Protocol digest (Source role)

A session has three phases. For FluxCast only the third is new code.

1. **Device discovery.** Windows discovers sinks via Wi-Fi Direct probe
   responses and reads a WSC Vendor Extension attribute (MS-MICE §2.2.8)
   for infrastructure capability, host name, and optional sink IP
   addresses. Sinks ALSO register an mDNS service (MS-MICE §3.1.1):
   `<friendly name>._display._tcp.local`, SRV port 7250, TXT
   `container_id=<GUID>` (Microsoft's receiver-implementation guidance
   makes mDNS mandatory). FluxCast browses mDNS directly — no Wi-Fi
   Direct involvement at all — and resolves the sink address via
   the SRV record. Falls back to standard WFD when no `_display._tcp`
   service is found.
2. **Host name resolution.** Skipped: the SRV record already carries the
   address and port.
3. **Projection.** The Source connects TCP to the sink's port 7250 and
   exchanges MS-MICE messages; the sink then connects back to the
   Source's RTSP control port (7236) and the standard WFD RTSP/RTP
   session proceeds unchanged over the infrastructure.

### Message framing (MS-MICE §2.2)

All multi-byte integers are big-endian.

```
+------+------+---------+
| Size | Ver  | Command | TLVArray...
| 2B   | 1B   | 1B      | variable
+------+------+---------+
```

- `Size` — total message size in bytes, including the header.
- `Version` — 0x01.
- `Command` — one of:

| Command | Value | Direction | Purpose |
| --- | --- | --- | --- |
| SOURCE_READY | 0x01 | Source→Sink | Source is listening on its RTSP port |
| STOP_PROJECTION | 0x02 | both | Graceful end of session |
| SECURITY_HANDSHAKE | 0x03 | both | DTLS handshake (optional) |
| SESSION_REQUEST | 0x04 | Source→Sink | Announce intent + security options (optional) |
| PIN_CHALLENGE | 0x05 | Source→Sink | PIN validation (optional) |
| PIN_RESPONSE | 0x06 | Sink→Source | PIN result (optional) |

### TLVs (MS-MICE §2.2.7)

Each TLV: `Type (1B) | Length (2B) | Value (Length B)`.

| TLV | Type | Value | Notes |
| --- | --- | --- | --- |
| FRIENDLY_NAME | 0x00 | UTF-8 string | mandatory in SOURCE_READY |
| RTSP_PORT | 0x02 | 2-byte port | mandatory in SOURCE_READY |
| SOURCE_ID | 0x03 | 4-byte id | mandatory in SOURCE_READY |
| SECURITY_TOKEN | 0x04 | DTLS message | optional (encryption) |
| SECURITY_OPTIONS | 0x05 | flags | optional (encryption/PIN) |
| PIN_CHALLENGE | 0x06 | salted hash | optional (PIN) |
| PIN_RESPONSE_REASON | 0x07 | 1-byte reason | optional (PIN) |

### Minimal Source sequence (no encryption, no PIN)

Per Microsoft's receiver guidance, only SOURCE_READY and
STOP_PROJECTION are mandatory for a Source; the sink treats the rest as
optional. The baseline flow:

1. Resolve the sink via mDNS (`_display._tcp.local`, SRV → ip:7250).
2. Start the RTSP server on 7236 (existing FluxCast code).
3. TCP connect to sink:7250.
4. Send SOURCE_READY {FRIENDLY_NAME, RTSP_PORT=7236, SOURCE_ID}.
5. Sink connects to our 7236; existing WFD RTSP session (M1–M7) and
   RTP streaming proceed exactly as in the P2P path.
6. On teardown, send STOP_PROJECTION and close the 7250 socket.

DTLS/PIN phases are deliberately out of scope for the first cut; sinks
must accept the unencrypted baseline (capability bits in the sink's
Vendor Extension govern what a Windows source *should* do, not what it
must). If a sink refuses unauthenticated sessions in practice, the
SECURITY_HANDSHAKE (DTLS over the same socket, §2.2.3) is the follow-up
work.

## Architecture in FluxCast

- `src/wfd/mice.py` — message codec (encode/decode, TLV pack/parse) +
  the Source-side client: `mice_connect(host, rtsp_port, friendly_name)`
  returning the 7250 socket, and `mice_stop(sock, source_id)`. Pure
  stdlib; no new hard dependencies.
- mDNS discovery — `zeroconf` (python-zeroconf) browsing
  `_display._tcp.local`, wrapped so it is imported lazily: the codec and
  TCP client work without it and `--doctor` reports it as optional the
  way it treats other backends' tools.
- Session integration — a third `--wfd-p2p-backend` choice `mice`
  (naming to be revisited: it is a *transport*, not a P2P backend):
  when selected, `session.py` skips peer scanning, P2P group formation,
  and IP configuration entirely; discovery goes through mDNS; the
  existing RTSP server and cleanup flow are reused as-is.
- Firewall note — the sink connects *back* to our 7236, so hosts need
  inbound 7250-out/7236-in on the LAN. FluxCast's `firewall.py` already
  opens 7236 per-session where it can (firewalld/ufw); the NixOS-side
  deployment opens 7236 on the wired/Wi-Fi interfaces.

## Test plan

- Unit: codec round-trips (header, each TLV, SOURCE_READY build,
  STOP_PROJECTION build, decoder tolerance of TLV order).
- Unit: mock-socket client sequence.
- Integration (needs hardware, i.e. the office conference rooms):
  mDNS browse finds the MWDA sinks; full session against
  Conference_Room_3 over the wired LAN.

[MS-MICE]: https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-mice/9598ca72-d937-466c-95f6-70401bb10bdb
