"""
Global utility functions
"""

from __future__ import absolute_import
import os
import tempfile as tmp
import fcntl
from contextlib import contextmanager
from prometheus_client import CollectorRegistry, Gauge, write_to_textfile


def acquire_lock(lockfile):
    # lockfile should be an opened file in mode w

    try:
        fcntl.lockf(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        return False

    return True


@contextmanager
def tempfile(suffix='', dir=None):
    """ Context for temporary file.

    Will find a free temporary filename upon entering
    and will try to delete the file on leaving, even in case of an exception.

    Parameters
    ----------
    suffix : string
        optional file suffix
    dir : string
        optional directory to save temporary file in
    """

    tf = tmp.NamedTemporaryFile(delete=False, suffix=suffix, dir=dir)
    tf.file.close()
    try:
        yield tf.name
    finally:
        try:
            os.remove(tf.name)
        except OSError as e:
            if e.errno == 2:
                pass
            else:
                raise


@contextmanager
def open_atomic(filepath, fsync=False, **kwargs):
    """ Open temporary file object that atomically moves to destination upon
    exiting.

    Allows reading and writing to and from the same filename.

    The file will not be moved to destination in case of an exception.

    Parameters
    ----------
    filepath : string
        the file path to be opened
    fsync : bool
        whether to force write the file to disk
    **kwargs : mixed
        Any valid keyword arguments for :code:`open`
    """

    with tempfile(dir=os.path.dirname(os.path.abspath(filepath))) as tmppath:
        with open(tmppath, **kwargs) as file:
            try:
                yield file
            finally:
                if fsync:
                    file.flush()
                    os.fsync(file.fileno())
        os.rename(tmppath, filepath)

class SingletonMetaclass(type):
    """
    Singleton metaclass to enable Monitor class
    to be used accross different modules
    """
    _instances = {}
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        # return instance so we can call Monitor().set()
        return cls._instances[cls]


class Monitor(metaclass=SingletonMetaclass):
    """
    Prometheus metrics monitor
    """

    # gauges array with key like: edge1_dev_deflect_network_response_time
    gauges = {}
    suffixs = [
        'response_time',
        'average_time',
        'timeslice',
        'reachable_status',
        'in_rotation',
    ]

    def __init__(self, edges, registry=None):
        if registry is None:
            self.registry = CollectorRegistry()
        self.create_gauges(edges)

    def create_gauges(self, edges):
        for edge in edges:
            for suffix in self.suffixs:
                key= f"{self._format(edge)}_{suffix}"
                self.gauges[key] = Gauge(key, '', registry=self.registry)

    def set(self, edge, suffix, value):
        self.gauges[f"{self._format(edge)}_{suffix}"].set(value)

    def inc(self, edge, suffix):
        self.gauges[f"{self._format(edge)}_{suffix}"].inc()

    def _format(self, edge):
        # Replace . in edge URL so it can be used as a metric name
        return edge.replace('.', '_')

    def write_metrics(self, filepath):
        try:
            write_to_textfile(filepath, self.registry)
        except FileNotFoundError as e:
            print(str(e))
            print("Please set path at 'prometheus_logs' in edgemanage.conf")
