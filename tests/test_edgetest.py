#!/usr/bin/env python

from __future__ import absolute_import
import socket
import unittest
from unittest import mock

from .context import edgemanage
from edgemanage.edgemanage import future_fetch

EDGE = "edge1.example.com"
EAI_AGAIN = socket.gaierror(-3, "Temporary failure in name resolution")


class EdgeTestResolutionTest(unittest.TestCase):

    def test_resolution_failure_is_a_failed_fetch(self):
        edge_t = edgemanage.EdgeTest(EDGE, "abc")
        with mock.patch("edgemanage.edgetest.socket.gethostbyname",
                        side_effect=EAI_AGAIN) as gethostbyname, \
                mock.patch.object(edge_t, "make_request") as make_request:
            result = edge_t.fetch("example.com", "/test.txt")

        self.assertEqual(result, edgemanage.const.FETCH_TIMEOUT)
        # Retried once, then gave up without making a request
        self.assertEqual(gethostbyname.call_count, 2)
        make_request.assert_not_called()

    def test_resolution_retry_recovers(self):
        edge_t = edgemanage.EdgeTest(EDGE, "abc")
        with mock.patch("edgemanage.edgetest.socket.gethostbyname",
                        side_effect=[EAI_AGAIN, "192.0.2.1"]), \
                mock.patch.object(edge_t, "make_request") as make_request:
            make_request.return_value.ok = False
            make_request.return_value.text = "nope"
            self.assertRaises(edgemanage.FetchFailed, edge_t.fetch,
                              "example.com", "/test.txt")

        self.assertEqual(make_request.call_args[0][0], "192.0.2.1")


class FutureFetchTest(unittest.TestCase):

    def test_unexpected_exception_is_a_failed_fetch(self):
        # Any exception escaping EdgeTest.fetch used to leave fetch_result
        # unset, so the UnboundLocalError killed the whole run
        edge_t = edgemanage.EdgeTest(EDGE, "abc")
        with mock.patch.object(edge_t, "fetch", side_effect=EAI_AGAIN):
            result = future_fetch(edge_t, "example.com", "/test.txt", "https", 443, False)

        self.assertEqual(result, {EDGE: (edgemanage.const.FETCH_TIMEOUT, "fetch_failed")})


if __name__ == '__main__':
    unittest.main()
