import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd import mice_discovery  # noqa: E402
from wfd.config import WFDNotReady  # noqa: E402

SERVICE_TYPE = mice_discovery.MICE_SERVICE_TYPE
CONFERENCE = f"Conference_Room_3.{SERVICE_TYPE}"
BOARDROOM = f"Service_Center_Board_Room.{SERVICE_TYPE}"


class FakeServiceInfo:
    def __init__(self, port=7250, addresses=None, properties=None, server=None):
        self.port = port
        self.addresses = addresses or [bytes([10, 50, 56, 10])]
        self.properties = properties or {}
        self.server = server

    def parsed_addresses(self):
        return [f"{b[0]}.{b[1]}.{b[2]}.{b[3]}" for b in self.addresses]


class FakeZeroconf:
    """Resolves names from a canned table; records close()."""

    def __init__(self, infos=None):
        self._infos = dict(infos or {})
        self.closed = False

    def get_service_info(self, service_type, name, timeout=None):
        return self._infos.get(name)

    def close(self):
        self.closed = True


class FakeServiceBrowser:
    """Records its construction; callbacks are driven by the test."""

    def __init__(self, zeroconf, service_type, listener=None):
        self.zeroconf = zeroconf
        self.service_type = service_type
        self.listener = listener


def install_fake_zeroconf(test, drive=lambda zc, st, listener: None):
    """Patch sys.modules['zeroconf'] for the module's lazy import.

    `drive` runs when ServiceBrowser would be constructed - i.e. inside
    browse_mice_sinks - and fires listener callbacks against the fake
    zeroconf, standing in for the engine threads real zeroconf uses.
    """
    made = {}

    def make_browser(zc, service_type, listener=None):
        drive(zc, service_type, listener)
        made["browser"] = FakeServiceBrowser(zc, service_type, listener)
        return made["browser"]

    module = types.ModuleType("zeroconf")
    module.Zeroconf = lambda: test.zeroconf
    module.ServiceBrowser = make_browser
    patcher = mock.patch.dict(sys.modules, {"zeroconf": module})
    patcher.start()
    test.addCleanup(patcher.stop)
    test.made = made


def announce(name):
    def drive(zc, service_type, listener):
        listener.add_service(zc, service_type, name)
    return drive


class TestBrowseMiceSinks(unittest.TestCase):
    def setUp(self):
        self.zeroconf = FakeZeroconf()
        self.made = {}

    def test_missing_zeroconf_raises_ready_error(self):
        with mock.patch.dict(sys.modules, {"zeroconf": None}):
            # A None entry makes "import zeroconf" fail outright.
            with self.assertRaises(WFDNotReady) as ctx:
                mice_discovery.browse_mice_sinks(timeout=0.1)
        self.assertIn("zeroconf", str(ctx.exception))

    def test_collects_sinks_with_txt_and_srv_data(self):
        self.zeroconf = FakeZeroconf(infos={
            CONFERENCE: FakeServiceInfo(
                addresses=[bytes([10, 50, 56, 47])],
                properties={b"container_id": b"{1111-2222}"},
                server="conference-room-3.local.",
            ),
            BOARDROOM: FakeServiceInfo(
                addresses=[bytes([10, 50, 56, 48])],
                properties={"container_id": "{3333-4444}"},  # str keys
            ),
        })

        def drive(zc, service_type, listener):
            listener.add_service(zc, service_type, CONFERENCE)
            listener.add_service(zc, service_type, BOARDROOM)

        install_fake_zeroconf(self, drive)
        sinks = mice_discovery.browse_mice_sinks(timeout=0.2)

        self.assertTrue(self.zeroconf.closed)
        self.assertEqual(self.made["browser"].service_type, SERVICE_TYPE)
        self.assertEqual([s.name for s in sinks],
                         ["Conference_Room_3", "Service_Center_Board_Room"])
        by_name = {s.name: s for s in sinks}
        self.assertEqual(by_name["Conference_Room_3"].host, "10.50.56.47")
        self.assertEqual(by_name["Conference_Room_3"].port, 7250)
        self.assertEqual(by_name["Conference_Room_3"].container_id,
                         "{1111-2222}")
        self.assertEqual(by_name["Conference_Room_3"].server,
                         "conference-room-3.local.")
        self.assertEqual(by_name["Service_Center_Board_Room"].container_id,
                         "{3333-4444}")
        self.assertEqual(by_name["Service_Center_Board_Room"].host,
                         "10.50.56.48")

    def test_duplicate_announcements_collapse_by_container_id(self):
        self.zeroconf = FakeZeroconf(infos={
            CONFERENCE: FakeServiceInfo(
                properties={b"container_id": b"{dup}"}),
        })

        def drive(zc, service_type, listener):
            listener.add_service(zc, service_type, CONFERENCE)
            listener.add_service(zc, service_type, CONFERENCE)

        install_fake_zeroconf(self, drive)
        self.assertEqual(
            len(mice_discovery.browse_mice_sinks(timeout=0.2)), 1)

    def test_removal_drops_the_sink(self):
        self.zeroconf = FakeZeroconf(infos={
            CONFERENCE: FakeServiceInfo(properties={b"container_id": b"{x}"}),
        })

        def drive(zc, service_type, listener):
            listener.add_service(zc, service_type, CONFERENCE)
            listener.remove_service(zc, service_type, CONFERENCE)

        install_fake_zeroconf(self, drive)
        self.assertEqual(mice_discovery.browse_mice_sinks(timeout=0.2), [])

    def test_unresolvable_service_is_skipped(self):
        install_fake_zeroconf(self, announce(CONFERENCE))  # no info canned
        self.assertEqual(mice_discovery.browse_mice_sinks(timeout=0.2), [])

    def test_update_refreshes_addresses(self):
        self.zeroconf = FakeZeroconf(infos={
            CONFERENCE: FakeServiceInfo(properties={b"container_id": b"{x}"}),
        })

        def drive(zc, service_type, listener):
            listener.add_service(zc, service_type, CONFERENCE)
            zc._infos[CONFERENCE] = FakeServiceInfo(
                addresses=[bytes([10, 50, 56, 99])],
                properties={b"container_id": b"{x}"},
            )
            listener.update_service(zc, service_type, CONFERENCE)

        install_fake_zeroconf(self, drive)
        sinks = mice_discovery.browse_mice_sinks(timeout=0.2)
        self.assertEqual(sinks[0].host, "10.50.56.99")

    def test_non_display_services_ignored(self):
        install_fake_zeroconf(
            self, announce("other._http._tcp.local."))
        self.assertEqual(mice_discovery.browse_mice_sinks(timeout=0.2), [])

    def test_empty_network_returns_empty_list(self):
        install_fake_zeroconf(self)
        self.assertEqual(mice_discovery.browse_mice_sinks(timeout=0.2), [])


class TestHelpers(unittest.TestCase):
    def test_friendly_name_strips_suffix(self):
        self.assertEqual(
            mice_discovery._friendly_name(CONFERENCE, SERVICE_TYPE),
            "Conference_Room_3")
        self.assertEqual(
            mice_discovery._friendly_name("bare", SERVICE_TYPE), "bare")

    def test_txt_value_handles_bytes_and_str(self):
        self.assertEqual(
            mice_discovery._txt_value({b"k": b"v"}, "k"), "v")
        self.assertEqual(
            mice_discovery._txt_value({"k": "v"}, "k"), "v")
        self.assertIsNone(mice_discovery._txt_value({}, "k"))

    def test_parsed_addresses_falls_back_to_raw_bytes(self):
        info = types.SimpleNamespace(addresses=[bytes([192, 168, 1, 2])])
        self.assertEqual(
            mice_discovery._parsed_addresses(info), ("192.168.1.2",))

    def test_host_prefers_address_over_local_name(self):
        sink = mice_discovery.MiceSink(
            name="x", addresses=("10.0.0.9",), port=7250,
            container_id=None, server="x.local.")
        self.assertEqual(sink.host, "10.0.0.9")
        bare = mice_discovery.MiceSink(
            name="x", addresses=(), port=7250,
            container_id=None, server="x.local.")
        self.assertEqual(bare.host, "x.local.")


if __name__ == "__main__":
    unittest.main()
