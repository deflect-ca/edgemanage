#!/usr/bin/env python

from __future__ import absolute_import
import json
import os
import shutil
import tempfile
import time
import unittest

from .context import edgemanage

DNET = "timednet"
GOOD_ENOUGH = 1.0
EDGES = ["edge1", "edge2", "edge3", "edge4"]
TEST_OBJECT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "test_data", "edge_test_object.txt")

# (seconds ago, fetch time) pairs that land an edge in each health tier
# with a goodenough of 1.0. Every run gets fresh EdgeStates, so pass_window
# needs an older fast fetch to pull the time slice average under 1.0.
HEALTH_FETCHES = {
    "pass_threshold": [(0, 0.1)],
    "pass_window": [(10, 0.1), (0, 1.5)],
    "pass": [(0, 2.0)],
    "fail": [(0, edgemanage.const.FETCH_TIMEOUT)],
}


class TimeRotationTest(unittest.TestCase):

    def setUp(self):
        self.store_dir = tempfile.mkdtemp()
        # No zone templates, so make_edges_live() only selects edges and
        # sets state.
        self.zone_dir = tempfile.mkdtemp()
        self.config = {
            "testobject": {"local": TEST_OBJECT},
            "goodenough": GOOD_ENOUGH,
            "edge_count": 4,
            "dnet_edge_count": {},
            "dnet_rotation_minutes": {DNET: 10},
            "zonetemplate_dir": self.zone_dir,
        }

    def tearDown(self):
        shutil.rmtree(self.store_dir)
        shutil.rmtree(self.zone_dir)

    def _run(self, state, healths, modes=None, dnet=DNET):
        """
        One edge_manage run: a fresh EdgeManage and EdgeStates, judged the
        way do_edge_tests() does it, then the state bookkeeping that
        edge_manage's main() does afterwards.

        Returns the live edges and make_edges_live()'s return value.
        """
        modes = modes or {}
        em = edgemanage.EdgeManage(dnet, self.config, state, dry_run=True)
        for edgename, health in healths.items():
            edge_state = edgemanage.EdgeState(edgename, self.store_dir, nowrite=True)
            for age, fetch_time in HEALTH_FETCHES[health]:
                edge_state.add_value(fetch_time, timestamp=time.time() - age)
            edge_state.mode = modes.get(edgename, "available")
            em.edge_states[edgename] = edge_state
            if edge_state.mode != "unavailable":
                em.decision.add_edge_state(edge_state)

        changed = em.make_edges_live(False)

        live = em.edgelist_obj.get_live_edges()
        if live != state.last_live:
            state.add_rotation(edgemanage.const.STATE_HISTORICAL_ROTATIONS)
        state.last_live = live
        return live, changed

    def _state(self, last_live, due, rotation_cycle=None):
        state = edgemanage.StateFile()
        state.last_live = sorted(last_live)
        state.rotation_list = [time.time() - (3600 if due else 0)]
        state.rotation_cycle = list(rotation_cycle or last_live)
        return state

    def _healthy(self, edges=EDGES):
        return dict((edge, "pass_threshold") for edge in edges)

    def test_edge_count_is_one(self):
        self.config["dnet_edge_count"] = {DNET: 4}
        state = edgemanage.StateFile()

        live, changed = self._run(state, self._healthy())

        self.assertEqual(len(live), 1)
        self.assertTrue(changed)
        self.assertEqual(state.rotation_cycle, live)

    def test_not_due_keeps_healthy_edge(self):
        state = edgemanage.StateFile()
        first, _ = self._run(state, self._healthy())

        second, changed = self._run(state, self._healthy())

        self.assertEqual(second, first)
        self.assertFalse(changed)
        self.assertEqual(state.rotation_cycle, first)

    def test_due_rotates_healthy_edge(self):
        state = edgemanage.StateFile()
        first, _ = self._run(state, self._healthy())
        state.rotation_list = [time.time() - 3600]

        second, changed = self._run(state, self._healthy())

        self.assertEqual(len(second), 1)
        self.assertNotEqual(second, first)
        self.assertTrue(changed)
        self.assertEqual(state.rotation_cycle, first + second)

    def test_full_cycle_before_repeat(self):
        state = edgemanage.StateFile()
        picks = []
        for _ in range(len(EDGES)):
            state.rotation_list = [time.time() - 3600]
            live, _ = self._run(state, self._healthy())
            picks.extend(live)

        # Every edge once, in the order they went live
        self.assertEqual(sorted(picks), EDGES)
        self.assertEqual(state.rotation_cycle, picks)

        # The next cycle starts from the edge that closed the last one and
        # visits every other edge before any of them repeats.
        last_of_cycle = picks[-1]
        next_picks = []
        for _ in range(len(EDGES) - 1):
            state.rotation_list = [time.time() - 3600]
            live, _ = self._run(state, self._healthy())
            next_picks.extend(live)

        self.assertNotIn(last_of_cycle, next_picks)
        self.assertEqual(sorted(next_picks), sorted(set(EDGES) - set([last_of_cycle])))
        self.assertEqual(state.rotation_cycle, [last_of_cycle] + next_picks)

    def test_best_tier_beats_unused_lower_tier(self):
        # edge2 is the only other pass_threshold edge and has already been
        # used, but it still wins over the unused slower edges.
        state = self._state(["edge1"], due=True, rotation_cycle=["edge1", "edge2"])
        healths = {"edge1": "pass_threshold", "edge2": "pass_threshold",
                   "edge3": "pass_window", "edge4": "pass"}

        live, changed = self._run(state, healths)

        self.assertEqual(live, ["edge2"])
        self.assertTrue(changed)
        # A new cycle, started from the edge we rotated away from
        self.assertEqual(state.rotation_cycle, ["edge1", "edge2"])

    def test_due_with_no_other_passing_edge_stays(self):
        state = self._state(["edge1"], due=True)
        rotation_list = list(state.rotation_list)
        healths = {"edge1": "pass_threshold", "edge2": "fail", "edge3": "fail"}

        live, changed = self._run(state, healths)

        self.assertEqual(live, ["edge1"])
        self.assertFalse(changed)
        self.assertEqual(state.rotation_cycle, ["edge1"])
        # No rotation recorded, so the next run tries again
        self.assertEqual(state.rotation_list, rotation_list)

    def test_not_due_keeps_pass_window_over_pass(self):
        state = self._state(["edge1"], due=False)
        healths = {"edge1": "pass_window", "edge2": "pass", "edge3": "pass"}

        live, changed = self._run(state, healths)

        self.assertEqual(live, ["edge1"])
        self.assertFalse(changed)

    def test_not_due_moves_to_better_tier(self):
        state = self._state(["edge1"], due=False)
        healths = {"edge1": "pass_window", "edge2": "pass_threshold", "edge3": "pass"}

        live, changed = self._run(state, healths)

        self.assertEqual(live, ["edge2"])
        self.assertTrue(changed)

    def test_not_due_fails_over_from_failing_edge(self):
        state = self._state(["edge1"], due=False)
        last_rotation = state.last_rotation()
        healths = self._healthy()
        healths["edge1"] = "fail"

        live, changed = self._run(state, healths)

        self.assertEqual(len(live), 1)
        self.assertNotEqual(live, ["edge1"])
        self.assertTrue(changed)
        # The failover restarts the timer
        self.assertGreater(state.last_rotation(), last_rotation)

    def test_unavailable_live_edge_with_everything_else_failing(self):
        state = self._state(["edge1"], due=False)
        healths = {"edge1": "pass_threshold", "edge2": "fail", "edge3": "fail"}

        live, changed = self._run(state, healths, modes={"edge1": "unavailable"})

        # The last resort, the same as a normal dnet: better a failing edge
        # than no A records at all.
        self.assertEqual(live, ["edge1"])
        self.assertFalse(changed)

    def test_everything_failing_readds_last_live(self):
        state = self._state(["edge1"], due=True)
        healths = dict((edge, "fail") for edge in EDGES)

        live, changed = self._run(state, healths)

        self.assertEqual(live, ["edge1"])
        self.assertFalse(changed)

    def test_forced_live_edge_suspends_rotation(self):
        state = self._state(["edge1"], due=True)

        live, changed = self._run(state, self._healthy(), modes={"edge1": "force"})

        self.assertEqual(live, ["edge1"])
        self.assertFalse(changed)

    def test_forced_edge_replaces_live_edge(self):
        state = self._state(["edge1"], due=False)

        live, changed = self._run(state, self._healthy(), modes={"edge3": "force"})

        self.assertEqual(live, ["edge3"])
        self.assertTrue(changed)

    def test_failing_forced_edge_does_not_suspend_rotation(self):
        state = self._state(["edge1"], due=True)
        healths = self._healthy()
        healths["edge3"] = "fail"

        live, _ = self._run(state, healths, modes={"edge3": "force"})

        self.assertEqual(len(live), 1)
        self.assertNotIn(live[0], ["edge1", "edge3"])

    def test_several_blindforced_edges_serve_one(self):
        state = self._state(["edge1"], due=False)
        healths = self._healthy()
        healths["edge3"] = "fail"

        live, _ = self._run(state, healths,
                            modes={"edge2": "blindforce", "edge3": "blindforce"})

        self.assertEqual(live, ["edge2"])

    def test_switch_from_normal_dnet_keeps_one_edge(self):
        # First run after adding a dnet to dnet_rotation_minutes: four edges
        # were live, and the healthiest of them is kept.
        state = self._state(EDGES, due=False)
        healths = {"edge1": "pass_window", "edge2": "pass", "edge3": "pass_threshold",
                   "edge4": "pass_window", "edge5": "pass_threshold"}

        live, changed = self._run(state, healths)

        self.assertEqual(live, ["edge3"])
        self.assertTrue(changed)

    def test_invalid_values_use_normal_selection(self):
        for value in (0, -5, "10", 1.5, True, None):
            self.config["dnet_rotation_minutes"] = {DNET: value}
            live, _ = self._run(edgemanage.StateFile(), self._healthy())
            self.assertEqual(len(live), 4, "value %r" % (value,))

    def test_missing_or_empty_config_key(self):
        del self.config["dnet_rotation_minutes"]
        live, _ = self._run(edgemanage.StateFile(), self._healthy())
        self.assertEqual(len(live), 4)

        # An empty YAML key loads as None
        self.config["dnet_rotation_minutes"] = None
        live, _ = self._run(edgemanage.StateFile(), self._healthy())
        self.assertEqual(len(live), 4)

    def test_other_dnet_uses_normal_selection(self):
        healths = self._healthy(EDGES + ["edge5"])
        healths["edge5"] = "fail"

        live, _ = self._run(edgemanage.StateFile(), healths, dnet="othernet")

        self.assertEqual(live, EDGES)

    def test_rotation_due_grace(self):
        interval = 600
        em = edgemanage.EdgeManage(DNET, self.config, edgemanage.StateFile(), dry_run=True)
        self.assertTrue(em.time_rotation_due(interval))

        # The run that lands a few seconds short of 10 minutes still rotates
        em.state_obj.rotation_list = [time.time() - interval + 5]
        self.assertTrue(em.time_rotation_due(interval))

        em.state_obj.rotation_list = [time.time() - interval + 60]
        self.assertFalse(em.time_rotation_due(interval))


class StateFileRotationCycleTest(unittest.TestCase):

    def test_default(self):
        self.assertEqual(edgemanage.StateFile().rotation_cycle, [])

    def test_old_statefile_without_key(self):
        state = edgemanage.StateFile({"last_live": ["edge1"], "rotation_list": [1.0]})
        self.assertEqual(state.rotation_cycle, [])

    def test_round_trip(self):
        state = edgemanage.StateFile()
        state.rotation_cycle = ["edge2", "edge1"]
        reloaded = edgemanage.StateFile(json.loads(state.to_json()))
        self.assertEqual(reloaded.rotation_cycle, ["edge2", "edge1"])


if __name__ == '__main__':
    unittest.main()
