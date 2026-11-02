#!/usr/bin/env python3
"""wait_for_device_access: the BME280 self-test waits for udev to fix
/dev/i2c-1 permissions instead of failing at boot."""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))


class TestWaitForDeviceAccess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fake_hw import import_waterflow
        cls.w = import_waterflow()

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.remove, self.path)

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file permissions')
    def test_waits_until_permissions_change(self):
        os.chmod(self.path, 0o000)
        threading.Timer(0.05, os.chmod, (self.path, 0o660)).start()
        t0 = time.monotonic()
        self.assertTrue(self.w.wait_for_device_access(self.path, 2.0, poll_s=0.01))
        self.assertLess(time.monotonic() - t0, 1.0)

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file permissions')
    def test_gives_up_after_timeout(self):
        os.chmod(self.path, 0o000)
        self.addCleanup(os.chmod, self.path, 0o600)
        self.assertFalse(self.w.wait_for_device_access(self.path, 0.05, poll_s=0.01))

    def test_missing_device_returns_immediately(self):
        t0 = time.monotonic()
        self.assertFalse(self.w.wait_for_device_access('/nonexistent/i2c-9', 5.0))
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_accessible_device(self):
        self.assertTrue(self.w.wait_for_device_access(self.path, 0))


if __name__ == '__main__':
    unittest.main()
