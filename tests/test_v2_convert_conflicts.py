"""A Longhorn upgrade and a V2 disk conversion never overlap: the upgrade
restarts the instance managers the conversion depends on. A Longhorn upgrade
is a platform-upgrade of the longhorn component."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_storage_conflicts as CONFLICTS

CONVERTING = {"kind": "disk-v2-convert", "status": "running", "ref": {"node": "node-1", "phase": "erasing"}}
LONGHORN = {"kind": "platform-upgrade", "status": "running", "ref": {"component": "longhorn"}}
K3S = {"kind": "platform-upgrade", "status": "running", "ref": {"component": "cluster"}}


class LonghornUpgradeTests(unittest.TestCase):
    def test_a_longhorn_upgrade_waits_for_a_disk_conversion(self):
        with self.assertRaisesRegex(ValueError, "Finish disk preparation"):
            CONFLICTS.require_clear([CONVERTING], "platform-upgrade", {"component": "longhorn"})

    def test_a_disk_conversion_waits_for_a_longhorn_upgrade(self):
        with self.assertRaisesRegex(ValueError, "Finish disk preparation"):
            CONFLICTS.require_clear([LONGHORN], "disk-v2-convert", {"node": "node-1"})

    def test_a_finished_longhorn_upgrade_does_not_hold_a_conversion(self):
        CONFLICTS.require_clear([dict(LONGHORN, status="succeeded")], "disk-v2-convert", {"node": "node-1"})

    def test_other_platform_upgrades_are_not_held_by_this_rule(self):
        CONFLICTS.require_clear([CONVERTING], "platform-upgrade", {"component": "metallb"})
        CONFLICTS.require_clear([K3S], "disk-v2-convert", {"node": "node-2"})


if __name__ == "__main__":
    unittest.main()
