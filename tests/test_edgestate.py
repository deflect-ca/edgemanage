#!/usr/bin/env python

from __future__ import absolute_import
import unittest
import tempfile
import shutil
import time

from .context import edgemanage
from six.moves import range

TEST_EDGE = "testedge1"
TEST_FETCH_HISTORY = 4
# Patch edgestate's import of const
edgemanage.edgestate.FETCH_HISTORY = TEST_FETCH_HISTORY


class EdgeStateTemplate(unittest.TestCase):
    """
    Sub-classable test to handle state file generation and cleanup
    """

    def _make_store(self):
        self.store_dir = tempfile.mkdtemp()
        a = edgemanage.edgestate.EdgeState(TEST_EDGE, self.store_dir)
        return a

    def _reopen_store(self, edgename, store_dir=None):
        if not store_dir:
            store_dir = self.store_dir
        b = edgemanage.edgestate.EdgeState(edgename, store_dir)
        return b

    def tearDown(self):
        shutil.rmtree(self.store_dir)


class EdgeStateTest(EdgeStateTemplate):

    # TODO load test JSON file to ensure object creation
    def testStoreAverage(self):
        a = self._make_store()

        for i in range(4):
            a.add_value(2)

        average = a.current_average()
        self.assertEqual(average, 2)

    def testStoreRotation(self):
        a = self._make_store()

        for i in range(TEST_FETCH_HISTORY + 1):
            a.add_value(2)
            time.sleep(0.01)

        self.assertEqual(len(a), TEST_FETCH_HISTORY)

    def testHistoricalAverageRotation(self):
        a = self._make_store()

        # A fixed datetime with minutes in zero is used to force the rotation
        minute_zero_ts = 1645210800  # 2022-02-18 19:00:00 UTC

        for i in range(TEST_FETCH_HISTORY * 2):
            a.add_value(2, timestamp=(minute_zero_ts + i / 100))

        self.assertEqual(len(a.historical_average), TEST_FETCH_HISTORY + 1)

    def testHistoricalAverageRotationAfterReloadingTheStateFile(self):
        """
        This test detects serialization issues when the rotation of
        historical average records is performed on reloaded files
        """

        a = self._make_store()
        minute_zero_ts = 1645210800  # 2022-02-18 19:00:00 UTC

        for i in range(TEST_FETCH_HISTORY * 2):
            a.add_value(2, timestamp=(minute_zero_ts + i / 100))

        b = self._reopen_store(a.edgename)
        minute_zero_ts = minute_zero_ts + 3600  # 2022-02-18 20:00:00 UTC
        for i in range(TEST_FETCH_HISTORY * 2):
            b.add_value(2, timestamp=(minute_zero_ts + i / 100))

        self.assertEqual(len(b.historical_average), TEST_FETCH_HISTORY + 1)


if __name__ == "__main__":
    unittest.main()
