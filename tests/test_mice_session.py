import argparse
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd import session  # noqa: E402
from wfd.config import WFDNotReady  # noqa: E402
from wfd.mice_discovery import MiceSink  # noqa: E402


def make_sink(name="Conference_Room_3", host="10.50.56.47", port=7250):
    return MiceSink(name=name, addresses=(host,), port=port,
                    container_id="{guid}", server=None)


def make_args(**overrides):
    defaults = dict(
        fps=30, bitrate="4M", output_res=None,
        wfd_transport="mice", wfd_peer="Conference", wfd_dry_run=False,
        wfd_test_pattern=True,  # skips monitor selection in tests
        wfd_no_firewall=True,   # skips firewall helpers
        wfd_timeout=1, wfd_rtsp_port=7236, wfd_rtp_source_port=19002,
        wfd_media_pipeline="auto", wfd_capture_backend="auto",
        wfd_no_audio=True, wfd_audio_device=None, wfd_ffmpeg_stats=False,
        wfd_latency_log=None, wfd_dump_ts=None, wfd_uibc=False,
        wfd_aosp_pmt_pid=False, bitrate_explicit=False,
        monitor_name=None, device_name=None, wfd_interface=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestSelectMiceSink(unittest.TestCase):
    def setUp(self):
        self.sinks = [make_sink("Conference_Room_3"),
                      make_sink("Service_Center_Board_Room", host="10.50.56.48")]

    def test_no_sinks_raises_ready_error(self):
        with self.assertRaises(WFDNotReady):
            session._select_mice_sink([], None)

    def test_index_selector(self):
        self.assertIs(
            session._select_mice_sink(self.sinks, "1"),
            self.sinks[1])

    def test_index_out_of_range(self):
        with self.assertRaises(WFDNotReady):
            session._select_mice_sink(self.sinks, "9")

    def test_name_substring_match(self):
        picked = session._select_mice_sink(self.sinks, "conference")
        self.assertEqual(picked.name, "Conference_Room_3")

    def test_no_match(self):
        with self.assertRaises(WFDNotReady):
            session._select_mice_sink(self.sinks, "lobby")

    def test_interactive_prompt_defaults_to_zero(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertIs(session._select_mice_sink(self.sinks, None),
                          self.sinks[0])


class TestStartMiceSession(unittest.TestCase):
    def setUp(self):
        self.sink = make_sink()
        rtsp_patcher = mock.patch.object(session, "WFDRTSPServer")
        self.rtsp_cls = rtsp_patcher.start()
        self.rtsp = self.rtsp_cls.return_value
        self.addCleanup(rtsp_patcher.stop)
        browse_patcher = mock.patch.object(
            session, "browse_mice_sinks", return_value=[self.sink])
        browse_patcher.start()
        self.addCleanup(browse_patcher.stop)
        connect_patcher = mock.patch.object(
            session, "mice_connect", return_value=mock.sentinel.sock)
        self.mice_connect = connect_patcher.start()
        self.addCleanup(connect_patcher.stop)
        stop_patcher = mock.patch.object(session, "mice_stop")
        self.mice_stop = stop_patcher.start()
        self.addCleanup(stop_patcher.stop)
        diag_patcher = mock.patch.object(
            session, "run_diagnostics", return_value=mock.Mock(wfd_candidate=False))
        diag_patcher.start()
        self.addCleanup(diag_patcher.stop)
        report_patcher = mock.patch.object(session, "print_report")
        report_patcher.start()
        self.addCleanup(report_patcher.stop)
        dump_patcher = mock.patch.object(session, "report_ts_dump")
        dump_patcher.start()
        self.addCleanup(dump_patcher.stop)
        probe_patcher = mock.patch.object(session, "_active_rtsp_probe")
        probe_patcher.start()
        self.addCleanup(probe_patcher.stop)

    def _run(self, args):
        # The wait loop runs until Ctrl+C; make the first sleep raise.
        with mock.patch.object(session.time, "sleep",
                               side_effect=KeyboardInterrupt):
            session.start_mice_session(args)

    def test_full_session_lifecycle(self):
        args = make_args()
        self._run(args)

        self.rtsp_cls.assert_called_once()
        self.assertEqual(self.rtsp_cls.call_args.kwargs["peer_address"],
                         "10.50.56.47")
        self.rtsp.start.assert_called_once()
        self.mice_connect.assert_called_once()
        kwargs = self.mice_connect.call_args
        self.assertEqual(kwargs.args[0], "10.50.56.47")      # host
        self.assertEqual(kwargs.args[1], 7236)               # rtsp port
        self.assertTrue(kwargs.args[2])                      # friendly name
        self.assertIsInstance(kwargs.args[3], int)           # source id
        self.assertEqual(kwargs.kwargs.get("sink_port"), 7250)
        # Teardown order: media, dump, rtsp, then MICE stop with the id.
        self.rtsp.stop_all_media.assert_called_once()
        self.rtsp.stop.assert_called_once()
        self.mice_stop.assert_called_once()
        self.assertIs(self.mice_stop.call_args.args[0], mock.sentinel.sock)
        self.assertEqual(self.mice_stop.call_args.args[1],
                         self.mice_connect.call_args.args[3])

    def test_dry_run_skips_connect(self):
        args = make_args(wfd_dry_run=True)
        session.start_mice_session(args)  # no KeyboardInterrupt needed
        self.mice_connect.assert_not_called()
        self.rtsp_cls.assert_not_called()

    def test_peer_selector_chooses_sink(self):
        args = make_args(wfd_peer="Conference")
        self._run(args)
        self.mice_connect.assert_called_once()
        self.assertEqual(self.mice_connect.call_args.args[0], "10.50.56.47")

    def test_no_sinks_raises_after_browse(self):
        with mock.patch.object(session, "browse_mice_sinks",
                               return_value=[]):
            with self.assertRaises(WFDNotReady):
                session.start_mice_session(make_args())
        self.mice_connect.assert_not_called()

    def test_connect_failure_untouched_socket(self):
        # mice_connect raising must still tear the RTSP server down.
        self.mice_connect.side_effect = OSError("refused")
        try:
            self._run(make_args())
        except OSError:
            pass
        self.rtsp.stop.assert_called_once()
        self.mice_stop.assert_not_called()


class TestMainDispatch(unittest.TestCase):
    """The CLI routing: --wfd-transport mice reaches the MICE session."""

    def test_scan_routes_to_mice_browse(self):
        import main as main_mod
        argv = ["main.py", "--protocol", "wfd", "--wfd-scan",
                "--wfd-transport", "mice"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(main_mod, "print") as fake_print, \
             mock.patch("wfd.browse_mice_sinks",
                        return_value=[make_sink()]) as browse, \
             mock.patch.object(main_mod.sys, "exit") as fake_exit:
            main_mod.main()
        browse.assert_called_once()
        fake_exit.assert_not_called()
        printed = " ".join(str(c) for c in fake_print.call_args_list)
        self.assertIn("Conference_Room_3", printed)

    def test_session_routes_to_mice_backend(self):
        import main as main_mod
        argv = ["main.py", "--protocol", "wfd", "--wfd-transport", "mice",
                "--wfd-peer", "Conference", "--wfd-test-pattern"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch("wfd.start_mice_session") as fake_session, \
             mock.patch("wfd.start_experimental_backend") as fake_p2p:
            main_mod.main()
        fake_session.assert_called_once()
        fake_p2p.assert_not_called()
        self.assertEqual(fake_session.call_args.args[0].wfd_peer,
                         "Conference")

    def test_default_transport_stays_p2p(self):
        import main as main_mod
        argv = ["main.py", "--protocol", "wfd"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch("wfd.start_experimental_backend") as fake_p2p:
            main_mod.main()
        fake_p2p.assert_called_once()
        args = fake_p2p.call_args.args[0]
        self.assertEqual(args.wfd_transport, "p2p")


if __name__ == "__main__":
    unittest.main()
