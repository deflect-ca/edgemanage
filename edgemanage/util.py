"""
Global utility functions
"""

from __future__ import absolute_import
import os
import logging
import subprocess
import tempfile as tmp
import time
import fcntl
from contextlib import contextmanager

# Seconds to wait at the end of a run for spawned commands to finish so
# that their output can be logged. Overridable with the
# command_reap_timeout config key.
COMMAND_REAP_TIMEOUT = 2.0


class DnetLogFilter(logging.Filter):
    """Stamp every log record with the dnet the current run operates on.

    Attached to handlers rather than to a logger so that records
    propagated from library loggers (requests, urllib3) are tagged too -
    a logger's filters don't apply to records it only propagates.
    """

    def __init__(self, dnet):
        logging.Filter.__init__(self)
        self.dnet = dnet if dnet else "-"

    def filter(self, record):
        record.dnet = self.dnet
        return True


def run_command_list(commands, hook_name):
    """
    Spawn every command in a hook without waiting for it to finish.

    stdout and stderr are piped so that reap_command_list can report
    them at the end of the run. Returns a list of tuples describing what
    was started, to be handed to reap_command_list.

    Args:
     commands: a list of command strings, or None
     hook_name: the config key the commands came from, for logging
    """

    pending = []

    if not commands:
        return pending

    for command in commands:
        # Subprocess wants a list. This will complicate things for
        # people using complex strings but for now that's too bad.
        command = command.split(" ")
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError as e:
            logging.error("Failed to run command %s: %s", command, str(e))
        except Exception as e:
            # ~* I want to be the very best, like no one ever was *~
            # I'll allow this kind of exception handling here
            # because an unforeseen condition here shouldn't break
            # execution.
            logging.error(
                "Caught unhandled exception when running command %s: %s",
                command, str(e)
            )
        else:
            logging.info("%s: spawned %s (pid %d)", hook_name,
                         " ".join(command), proc.pid)
            pending.append((hook_name, " ".join(command), proc))

    return pending


def reap_command_list(pending, timeout=COMMAND_REAP_TIMEOUT):
    """
    Collect the output and exit status of commands started by
    run_command_list.

    The timeout is a deadline shared by every command rather than a
    per-command wait, so a hook with several commands doesn't multiply
    the delay. Commands that are still running when it expires are
    logged and left alone - we never wait on a hook and never kill one.
    """

    if not pending:
        return

    deadline = time.monotonic() + timeout

    for hook_name, command, proc in pending:

        remaining = max(0, deadline - time.monotonic())

        try:
            stdout, stderr = proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            logging.warning("%s %s (pid %d) still running at the end of the "
                            "run; output not captured",
                            hook_name, command, proc.pid)
            continue

        # No runtime is reported because we never waited on the command -
        # all we know is that it finished some time between being spawned
        # and being reaped here.
        if proc.returncode == 0:
            logging.info("%s %s: exit 0", hook_name, command)
            log_command_output(logging.debug, stdout, stderr)
        else:
            logging.error("%s %s: exit %d", hook_name, command, proc.returncode)
            log_command_output(logging.error, stdout, stderr)


def log_command_output(log_func, stdout, stderr):
    """ Log whichever of a command's output streams aren't empty """

    for stream_name, stream in [("stdout", stdout), ("stderr", stderr)]:
        if not stream:
            continue
        stream = stream.decode("utf-8", "replace").strip()
        if stream:
            log_func("  %s: %s", stream_name, stream)


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
