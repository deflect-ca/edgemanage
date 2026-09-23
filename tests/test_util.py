#!/usr/bin/env python

from __future__ import absolute_import
import unittest
from .context import edgemanage

import os
import sys
import tempfile
import subprocess


class UtilTest(unittest.TestCase):

    '''Tests for any utility functions'''

    def test_acquire_lock(self, do_subproc=False):
        '''Test for file locking, ensure we can and then can't lock a temporary file.
        '''
        temp_file = tempfile.mktemp()
        with open(temp_file, "w") as temp_file_f:

            self.assertTrue(edgemanage.util.acquire_lock(temp_file_f),
                            msg="Couldn't lock temporary file")

            # ayyyy lmao. Gotta have another process to get an flock
            # refused. I am a bad person and you should hate me.
            #
            # This is even more disgusting now tests are in a separate directory!
            test_lock_fail = subprocess.Popen([
                "python", "-c", (
                    "import os; import sys; "
                    "sys.path.insert(0, os.path.abspath('..')); "
                    "import edgemanage; "
                    "ret = edgemanage.util.acquire_lock(open('%s', 'w')); "
                    "sys.exit(0 if not ret else 1)"
                ) % temp_file])
            returncode = test_lock_fail.wait()
            self.assertEqual(returncode, 0,
                             msg="Could lock already locked temporary file")


class CommandListTest(unittest.TestCase):

    '''Tests for the running and reaping of configured commands'''

    def _run_and_reap(self, command, timeout=5):
        with self.assertLogs(level="DEBUG") as logs:
            pending = edgemanage.util.run_command_list([command], "run_after")
            edgemanage.util.reap_command_list(pending, timeout)
        return pending, logs.output

    def test_no_commands(self):
        '''An unconfigured hook shouldn't spawn or reap anything'''
        self.assertEqual(edgemanage.util.run_command_list(None, "run_before"), [])
        self.assertEqual(edgemanage.util.run_command_list([], "run_before"), [])
        # Reaping nothing shouldn't log or explode
        self.assertIsNone(edgemanage.util.reap_command_list([]))

    def test_successful_command(self):
        '''A command that exits 0 is logged at info, its output at debug'''
        pending, output = self._run_and_reap("echo hello")

        self.assertEqual(len(pending), 1)
        self.assertTrue(any("run_after: spawned echo hello" in line
                            for line in output),
                        msg="Spawn wasn't logged at info: %s" % output)
        self.assertTrue(any(line.startswith("INFO") and "exit 0" in line
                            for line in output),
                        msg="Exit status wasn't logged at info: %s" % output)
        self.assertTrue(any(line.startswith("DEBUG") and "stdout: hello" in line
                            for line in output),
                        msg="Output wasn't logged at debug: %s" % output)

    def test_failing_command(self):
        '''A non-zero exit is logged at error along with stderr'''
        # Commands are split on spaces, so anything with arguments that
        # contain them has to go into a script.
        script_path = tempfile.mktemp(suffix=".py")
        with open(script_path, "w") as script_f:
            script_f.write("import sys\n"
                           "sys.stderr.write('boom')\n"
                           "sys.exit(3)\n")
        self.addCleanup(os.remove, script_path)

        _, output = self._run_and_reap("%s %s" % (sys.executable, script_path))

        self.assertTrue(any(line.startswith("ERROR") and "exit 3" in line
                            for line in output),
                        msg="Failure wasn't logged at error: %s" % output)
        self.assertTrue(any(line.startswith("ERROR") and "stderr: boom" in line
                            for line in output),
                        msg="stderr wasn't logged at error: %s" % output)

    def test_unfinished_command_is_not_killed(self):
        '''A command that outlives the reap timeout is warned about, not killed'''
        pending, output = self._run_and_reap("sleep 30", timeout=0.1)

        self.assertTrue(any(line.startswith("WARNING") and "still running" in line
                            for line in output),
                        msg="Unfinished command wasn't warned about: %s" % output)

        proc = pending[0][2]
        self.assertIsNone(proc.poll(), msg="Unfinished command was killed")
        proc.kill()
        proc.wait()

    def test_missing_binary(self):
        '''A command that can't be spawned is logged and not returned'''
        with self.assertLogs(level="ERROR") as logs:
            pending = edgemanage.util.run_command_list(
                ["/nonexistent/edgemanage-test-binary"], "run_after_changes")

        self.assertEqual(pending, [])
        self.assertTrue(any("Failed to run command" in line for line in logs.output),
                        msg="Missing binary wasn't logged: %s" % logs.output)


if __name__ == '__main__':
    unittest.main()
