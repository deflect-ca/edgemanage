"""
Module for making judgement on whether a givin edge passed threshold
"""

from __future__ import absolute_import
from . import const
from .util import Monitor

import logging
import time
import six


class DecisionMaker(object):

    """
    This object defines the rule of a edge's health,
    object is created in `edgemanage` and is able to
    provide judgement after some `add_edge_state()` call
    """

    def __init__(self):
        self.edge_states = {}
        # A results dict with edge as key, string as value, one of
        # VALID_HEALTHS
        self.current_judgement = {}
        self.edges_disabled = False

    def add_edge_state(self, edge_state):
        """
        Called by external module
        for adding new edge state
        """

        self.edge_states[edge_state.edgename] = edge_state
        self.current_judgement[edge_state.edgename] = None

    def get_judgement(self, edgename):
        """ Returns `current_judgement` of a edge """
        return self.current_judgement[edgename]

    def edge_is_passing(self, edgename):
        """ Returns `True` if `get_judgement` is not 'fail' """
        return self.get_judgement(edgename) != "fail"

    def edge_average(self, edgename):
        """ Returns `current_average()` of a edge """
        return self.edge_states[edgename].current_average()

    def edge_state_slice(self, edge_state):
        """
        Slice edge_state.fetch_times base on const.DECISION_SLICE_WINDOW

        Note: This is a fix for

            time_slice = edge_state[time.time() - const.DECISION_SLICE_WINDOW:time.time()]

        """
        sliced = {}
        upper_bound = time.time()
        lower_bound = upper_bound - const.DECISION_SLICE_WINDOW

        for ts, fetch_time in six.iteritems(edge_state.fetch_times):
            if float(ts) >= lower_bound and float(ts) <= upper_bound:
                sliced[ts] = fetch_time

        return sliced

    def check_threshold(self, good_enough):
        """
        Check fetch response times for being under the given
        threshold.

        Interate each edge, check them by this order:

        - edge_state.last_value() < good_enough
        - edge_state.last_value() == const.FETCH_TIMEOUT
        - time_slice and time_slice_avg < good_enough
        - edge_state.current_average() < good_enough
        - else

        """

        # dict for stats to return
        results_dict = {}
        for statusname in const.VALID_HEALTHS:
            results_dict[statusname] = 0

        # Set all as failed if this set of edges have been disabled
        if self.edges_disabled:
            for edgename in self.edge_states:
                results_dict["fail"] += 1
                self.current_judgement[edgename] = "fail"
            logging.info("FAIL: %d edges have been disabled", results_dict["fail"])
            return results_dict

        for edgename, edge_state in six.iteritems(self.edge_states):
            time_slice = self.edge_state_slice(edge_state)
            if time_slice:
                time_slice_avg = sum(time_slice.values()) / len(time_slice)
                logging.debug("Analysing %s. Last val: %f, time slice: %f, average: %f",
                              edgename, edge_state.last_value(), time_slice_avg,
                              edge_state.current_average())
                Monitor().set(edgename, "response_time", edge_state.last_value())
                Monitor().set(edgename, "average_time", edge_state.current_average())
                Monitor().set(edgename, "timeslice", time_slice_avg)
            else:
                time_slice_avg = None
                logging.debug("Analysing %s. Last val: %f, time slice: Not enough data, "
                              "average: %f",
                              edgename, edge_state.last_value(), edge_state.current_average())
                Monitor().set(edgename, "response_time", edge_state.last_value())
                Monitor().set(edgename, "average_time", edge_state.current_average())
                Monitor().set(edgename, "timeslice", -1)

            if edge_state.last_value() < good_enough:
                self.current_judgement[edgename] = "pass_threshold"
                results_dict["pass_threshold"] += 1
                logging.info("PASS: Last fetch for %s is under the good_enough threshold "
                             "(%f < %f)", edgename, edge_state.last_value(), good_enough)
                Monitor().set(edgename, "reachable_status", 1)
            elif edge_state.last_value() == const.FETCH_TIMEOUT:
                # FETCH_TIMEOUT must be checked before the average measurements. An edge
                # whose most recent fetch has failed should be marked as fail even if
                # the average value is still passing.
                self.current_judgement[edgename] = "fail"
                results_dict["fail"] += 1
                logging.info(("FAIL: Fetch time for %s is equal to the FETCH_TIMEOUT of %d. "
                              "Automatic fail"),
                             edgename, const.FETCH_TIMEOUT)
                Monitor().set(edgename, "reachable_status", 0)
            elif time_slice and time_slice_avg < good_enough:
                self.current_judgement[edgename] = "pass_window"
                results_dict["pass_window"] += 1
                logging.info("UNSURE: Last fetch for %s is NOT under the good_enough threshold "
                             "but the average of the last %d items is (%f < %f)",
                             edgename, len(time_slice), time_slice_avg, good_enough)
                Monitor().set(edgename, "reachable_status", 1)
            elif edge_state.current_average() < good_enough:
                self.current_judgement[edgename] = "pass_average"
                results_dict["pass_average"] += 1
                logging.info("UNSURE: Last fetch for %s is NOT under the good_enough threshold "
                             "but under the average (%f < %f)",
                             edgename, edge_state.current_average(), good_enough)
                Monitor().set(edgename, "reachable_status", 1)
            else:
                self.current_judgement[edgename] = "pass"
                results_dict["pass"] += 1
                logging.info("PASS: Last fetch for %s is not under the good_enough threshold "
                             "but is passing (%f < %f)", edgename,
                             edge_state.last_value(), const.FETCH_TIMEOUT)
                Monitor().set(edgename, "reachable_status", 1)

        return results_dict
