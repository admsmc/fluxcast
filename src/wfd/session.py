import random
import threading
import time
import types

from diagnostics import print_report, run_diagnostics

from .config import WFDMediaConfig, WFDNotReady
from .constants import _DEVICE_NAME, WFD_RTSP_PORT, WFD_UIBC_PORT
from .dump import report_ts_dump
from .env import _is_hyprland_session, _is_wayland_session
from .firewall import (
    _close_wfd_firewall_port, _open_wfd_firewall_port, _warn_if_ufw_may_block,
)
from .p2p.device import _set_p2p_device_name, _set_p2p_go_intent
from .p2p.nm import (
    _connect_peer, _deactivate_connection, _disconnect_device,
    _nm_p2p_device_path, _nm_p2p_uses_iwd, _wait_for_nm_activation,
)
from .mice import mice_connect, mice_stop
from .mice_discovery import MiceSink, browse_mice_sinks
from .p2p.peers import _scan_and_select
from .p2p.wpas import connect_via_wpa_supplicant, release_wpa_supplicant_connection
from .probe import _active_rtsp_probe
from .rtsp.rtsp_server import WFDRTSPServer


def _cleanup_step(label: str, action) -> None:
    """Run one teardown step without letting it skip the ones after it.

    Pressing Ctrl+C again while a session is being torn down used to abort the
    rest of the cleanup, which left the P2P connection up and the GO intent
    still lowered, so the next run needed a NetworkManager restart (#86).
    """
    try:
        action()
    except KeyboardInterrupt:
        print(f"[FluxCast WFD] Interrupted during {label}; finishing cleanup anyway.")
    except Exception as exc:
        print(f"[FluxCast WFD] Cleanup step '{label}' failed: {exc}")

def _select_monitor(args):
    """Shared monitor selection for both WFD transports; None in portal
    or test-pattern/dry-run modes where no choice is needed here."""
    if getattr(args, "wfd_test_pattern", False) or getattr(args, "wfd_dry_run", False):
        return None
    selected_backend = getattr(args, "wfd_capture_backend", "auto")
    portal_mode = selected_backend == "portal" or (
        selected_backend == "auto" and _is_wayland_session() and not _is_hyprland_session()
    )
    if portal_mode:
        print(
            "[FluxCast WFD] Portal backend: monitor selection will be done "
            "in the desktop portal dialog."
        )
        return None
    wfd_monitor_name = getattr(args, "monitor_name", None)
    if wfd_monitor_name:
        from capture import gather_monitors
        all_monitors = gather_monitors()
        monitor = next((m for m in all_monitors if m.name == wfd_monitor_name), None)
        if monitor is None:
            available = ", ".join(m.name for m in all_monitors) or "none"
            raise WFDNotReady(
                f"Monitor '{wfd_monitor_name}' not found. Available: {available}"
            )
        return monitor
    from capture import prompt_monitor
    return prompt_monitor()


def _select_mice_sink(sinks, selector):
    """Pick one MS-MICE sink: index or name substring when a selector is
    given (same semantics as --wfd-peer on the P2P path), interactive
    prompt otherwise."""
    if not sinks:
        raise WFDNotReady(
            "No Miracast over Infrastructure sinks found on this network. "
            "Check the sink is powered and on the LAN, then retry; or use "
            "the Wi-Fi Direct transport (default)."
        )
    if selector is None:
        print("[FluxCast WFD] Miracast over Infrastructure sink(s):")
        for idx, sink in enumerate(sinks):
            print(f"  [{idx}] {sink.name} @ {sink.host}:{sink.port}")
        try:
            raw = input("Select sink [0]: ").strip()
        except EOFError:
            raw = ""
        selector = raw or "0"
    if selector.isdigit():
        index = int(selector)
        if 0 <= index < len(sinks):
            return sinks[index]
        raise WFDNotReady(f"Sink index out of range: {selector}")
    normalized = selector.lower()
    for sink in sinks:
        if normalized in sink.name.lower():
            return sink
    raise WFDNotReady(f"No sink matched selector: {selector}")


def start_mice_session(args) -> None:
    """Miracast over Infrastructure session (MS-MICE): discover the sink
    via mDNS, tell it to call our RTSP control port, and stream over the
    infrastructure - no Wi-Fi Direct at any point. See mice.py and
    mice_discovery.py; the media/RTSP side is the existing WFD stack.
    """
    report = run_diagnostics(skip_firewall=getattr(args, "wfd_no_firewall", False))
    print_report(report)
    print()
    # No wfd_candidate gate here: that row tracks P2P readiness, and the
    # whole point of this transport is that none of it is needed.

    monitor = _select_monitor(args)

    sinks = browse_mice_sinks(timeout=getattr(args, "wfd_timeout", 8))
    sink = _select_mice_sink(sinks, getattr(args, "wfd_peer", None))
    print(f"[FluxCast WFD] Sink selected: {sink.name} @ {sink.host}:{sink.port}")

    rtsp_port = getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT)
    if getattr(args, "wfd_dry_run", False):
        print(
            f"[FluxCast WFD] Dry run: would connect MS-MICE to {sink.host}:{sink.port} "
            f"and advertise RTSP port {rtsp_port}."
        )
        return

    no_audio = getattr(args, "wfd_no_audio", False)
    if getattr(args, "wfd_test_pattern", False):
        if no_audio:
            print("[FluxCast WFD] Test pattern smoke mode is video-only (--wfd-no-audio).")
        else:
            print("[FluxCast WFD] Test pattern smoke mode includes AAC audio.")

    media_config = WFDMediaConfig(
        monitor=monitor,
        fps=args.fps,
        bitrate=args.bitrate,
        bitrate_explicit=getattr(args, "bitrate_explicit", False),
        output_resolution=args.output_res,
        audio_device=getattr(args, "wfd_audio_device", None),
        no_audio=no_audio,
        test_pattern=getattr(args, "wfd_test_pattern", False),
        ffmpeg_stats=getattr(args, "wfd_ffmpeg_stats", False),
        source_port=getattr(args, "wfd_rtp_source_port", 19002),
        media_pipeline=getattr(args, "wfd_media_pipeline", "auto"),
        latency_log_path=getattr(args, "wfd_latency_log", None),
        capture_backend=getattr(args, "wfd_capture_backend", "auto"),
        peer_name=sink.name,
        uibc=getattr(args, "wfd_uibc", False),
        aosp_pmt_pid=getattr(args, "wfd_aosp_pmt_pid", False),
        dump_ts_path=getattr(args, "wfd_dump_ts", None),
    )
    if media_config.dump_ts_path:
        print(f"[FluxCast WFD] Dumping transmitted MPEG-TS to: {media_config.dump_ts_path}")
    if media_config.latency_log_path:
        print(f"[FluxCast WFD] Latency log file: {media_config.latency_log_path}")

    device_name = getattr(args, "device_name", None) or _DEVICE_NAME
    # One id per process is enough; it only labels our messages (2.2.7.3).
    source_id = random.getrandbits(32)

    rtsp = WFDRTSPServer(
        media_config=media_config,
        peer_address=sink.host,
        port=rtsp_port,
    )
    mice_sock = None
    firewall_opened = False
    uibc_firewall_opened = False
    try:
        rtsp.start()
        mice_sock = mice_connect(
            sink.host,
            rtsp_port,
            device_name,
            source_id,
            sink_port=sink.port,
        )
        print(
            f"[FluxCast WFD] MS-MICE session open with {sink.name}; "
            f"waiting for it to connect back on RTSP port {rtsp_port}."
        )

        if not getattr(args, "wfd_no_firewall", False):
            uibc_enabled = getattr(args, "wfd_uibc", False)
            firewall_opened = _open_wfd_firewall_port(rtsp_port)
            if uibc_enabled:
                uibc_firewall_opened = _open_wfd_firewall_port(WFD_UIBC_PORT)
            ufw_ports = [(rtsp_port, "RTSP")]
            if uibc_enabled:
                ufw_ports.append((WFD_UIBC_PORT, "UIBC"))
            _warn_if_ufw_may_block(ufw_ports)

        # The P2P active probe expects a peer with address/rtsp_port; the
        # MICE sink's RTSP port is the sink's own 7250-side callback to
        # us, so advertise the standard control port instead.
        probe_peer = types.SimpleNamespace(
            address=sink.host, rtsp_port=rtsp_port)
        probe_thread = threading.Thread(
            target=_active_rtsp_probe,
            args=(rtsp, probe_peer, media_config),
            daemon=True,
        )
        probe_thread.start()

        print("[FluxCast WFD] Waiting for sink RTSP/WFD session. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[FluxCast WFD] Stopping WFD session...")
    finally:
        _cleanup_step("media shutdown", rtsp.stop_all_media)
        _cleanup_step("TS dump report",
                      lambda: report_ts_dump(media_config.dump_ts_path))
        _cleanup_step("RTSP server shutdown", rtsp.stop)
        if firewall_opened:
            _cleanup_step("firewall close", lambda: _close_wfd_firewall_port(rtsp_port))
        if uibc_firewall_opened:
            _cleanup_step("UIBC firewall close", lambda: _close_wfd_firewall_port(WFD_UIBC_PORT))
        if mice_sock is not None:
            _cleanup_step("MS-MICE stop projection",
                          lambda: mice_stop(mice_sock, source_id))


def start_experimental_backend(args) -> None:
    report = run_diagnostics(skip_firewall=getattr(args, "wfd_no_firewall", False))
    print_report(report)
    print()

    if not report.wfd_candidate:
        raise WFDNotReady(
            "Miracast/WFD is not ready on this machine yet. "
            "Fix the warn/fail rows above, then run --wfd-scan."
        )

    monitor = _select_monitor(args)

    backend_probe_path = _nm_p2p_device_path(args.wfd_interface)
    if not backend_probe_path:
        raise WFDNotReady(
            "NetworkManager did not expose a Wi-Fi P2P device before scanning."
        )

    using_iwd = _nm_p2p_uses_iwd(backend_probe_path)

    if using_iwd:
        print(
            "[FluxCast WFD] NetworkManager is using IWD; "
            "P2P device naming and GO intent stay under "
            "NetworkManager/IWD control."
        )
    else:
        _set_p2p_device_name(args.wfd_interface)

    peer = _scan_and_select(
        args.wfd_interface,
        getattr(args, "wfd_peer", None),
        args.wfd_timeout,
    )

    # Refresh the device after scanning. The scan may retry for tens of
    # seconds, so the path used for the actual connection should be fresh.
    device_path = _nm_p2p_device_path(args.wfd_interface)
    if not device_path:
        raise WFDNotReady(
            "NetworkManager P2P device disappeared before connection."
        )

    using_iwd = _nm_p2p_uses_iwd(device_path)

    if getattr(args, "wfd_dry_run", False):
        _connect_peer(
            device_path,
            peer,
            rtsp_port=getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT),
            dry_run=True,
        )
        return

    no_audio = getattr(args, "wfd_no_audio", False)
    if getattr(args, "wfd_test_pattern", False):
        if no_audio:
            print("[FluxCast WFD] Test pattern smoke mode is video-only (--wfd-no-audio).")
        else:
            print("[FluxCast WFD] Test pattern smoke mode includes AAC audio.")

    media_config = WFDMediaConfig(
        monitor=monitor,
        fps=args.fps,
        bitrate=args.bitrate,
        bitrate_explicit=getattr(args, "bitrate_explicit", False),
        output_resolution=args.output_res,
        audio_device=getattr(args, "wfd_audio_device", None),
        no_audio=no_audio,
        test_pattern=getattr(args, "wfd_test_pattern", False),
        ffmpeg_stats=getattr(args, "wfd_ffmpeg_stats", False),
        source_port=getattr(args, "wfd_rtp_source_port", 19002),
        media_pipeline=getattr(args, "wfd_media_pipeline", "auto"),
        latency_log_path=getattr(args, "wfd_latency_log", None),
        capture_backend=getattr(args, "wfd_capture_backend", "auto"),
        peer_name=peer.name,
        uibc=getattr(args, "wfd_uibc", False),
        aosp_pmt_pid=getattr(args, "wfd_aosp_pmt_pid", False),
        dump_ts_path=getattr(args, "wfd_dump_ts", None),
    )
    if media_config.dump_ts_path:
        print(f"[FluxCast WFD] Dumping transmitted MPEG-TS to: {media_config.dump_ts_path}")
    if media_config.latency_log_path:
        print(f"[FluxCast WFD] Latency log file: {media_config.latency_log_path}")

    rtsp_port = getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT)
    rtsp = WFDRTSPServer(
        media_config=media_config,
        peer_address=peer.address,
        port=rtsp_port,
    )
    firewall_opened = False
    uibc_firewall_opened = False
    active_path = ""
    previous_go_intent = None
    p2p_backend = getattr(args, "wfd_p2p_backend", "nm")
    wpas_data_iface = None
    try:
        # Clear stale P2P device state from previous runs before new activation.
        try:
            _disconnect_device(device_path)
        except Exception:
            pass
        rtsp.start()
        if p2p_backend == "wpas":
            # Bypasses NetworkManager's AddAndActivateConnection2 entirely -
            # see wpas.py's module docstring for why. connect_via_wpa_supplicant
            # handles GO-intent lowering internally, so it isn't done here.
            wpas_data_iface = connect_via_wpa_supplicant(
                args.wfd_interface, peer.address,
                go_intent=getattr(args, "wfd_go_intent", 0),
                rtsp_port=rtsp_port,
                p2p_channel=getattr(args, "wfd_p2p_channel", None),
                on_group_interface=rtsp.set_group_interface,
            )
        else:
            # Lower our GO intent before negotiation so the TV becomes the group
            # owner; most Miracast sinks only start the RTSP session in that role.
            if not using_iwd:
                previous_go_intent = _set_p2p_go_intent(
                    args.wfd_interface, getattr(args, "wfd_go_intent", 0)
                )

            active_path = _connect_peer(
                device_path,
                peer,
                rtsp_port=rtsp_port,
            )

            _wait_for_nm_activation(
                active_path,
                on_group_interface=rtsp.set_group_interface,
            )

        if not getattr(args, "wfd_no_firewall", False):
            uibc_enabled = getattr(args, "wfd_uibc", False)
            firewall_opened = _open_wfd_firewall_port(rtsp_port)
            if uibc_enabled:
                uibc_firewall_opened = _open_wfd_firewall_port(WFD_UIBC_PORT)

            # One hint per session, covering every port opened above.
            ufw_ports = [(rtsp_port, "RTSP")]
            if uibc_enabled:
                ufw_ports.append((WFD_UIBC_PORT, "UIBC"))
            _warn_if_ufw_may_block(ufw_ports)

        # Active probe for newer TVs (Samsung 2024++, some LGs)
        # It runs in a background thread to not block the main loop.
        probe_thread = threading.Thread(
            target=_active_rtsp_probe,
            args=(rtsp, peer, media_config),
            daemon=True,
        )
        probe_thread.start()

        print("[FluxCast WFD] Waiting for TV RTSP/WFD session. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[FluxCast WFD] Stopping WFD session...")
    finally:
        _cleanup_step("media shutdown", rtsp.stop_all_media)
        _cleanup_step("TS dump report",
                      lambda: report_ts_dump(media_config.dump_ts_path))
        _cleanup_step("RTSP server shutdown", rtsp.stop)
        if firewall_opened:
            _cleanup_step("firewall close", lambda: _close_wfd_firewall_port(rtsp_port))
        if uibc_firewall_opened:
            _cleanup_step("UIBC firewall close", lambda: _close_wfd_firewall_port(WFD_UIBC_PORT))
        if active_path:
            _cleanup_step("connection deactivate",
                          lambda: _deactivate_connection(active_path))
        if wpas_data_iface:
            _cleanup_step(
                "wpa_supplicant connection release",
                lambda: release_wpa_supplicant_connection(
                    args.wfd_interface, wpas_data_iface
                ),
            )
        _cleanup_step("P2P device disconnect", lambda: _disconnect_device(device_path))
        if previous_go_intent is not None:
            _cleanup_step(
                "GO intent restore",
                lambda: _set_p2p_go_intent(
                    args.wfd_interface, previous_go_intent, restoring=True
                ),
            )
