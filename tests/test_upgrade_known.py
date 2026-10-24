"""The upgrade check's knowledge table is well-formed: every row says where it
came from, and its versions read as versions (homestead_upgrade_known)."""
import re
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_upgrade_known as K

MINOR = re.compile(r"\d+\.\d+")


def minor(text):
    return tuple(int(part) for part in text.split("."))


class KnowledgeTableTests(unittest.TestCase):
    def test_the_table_says_when_it_was_checked(self):
        self.assertRegex(K.CHECKED, r"^\d{4}-\d{2}-\d{2}$")

    def test_every_row_has_an_https_source(self):
        self.assertTrue(K.KUBEVIRT_KUBERNETES["source"].startswith("https://"))
        for version, row in K.LONGHORN_CHARTS.items():
            self.assertTrue(row["source"].startswith("https://"), version)
            self.assertIn(version, row["source"], "a chart row cites that version's own chart")

    def test_versions_read_as_versions(self):
        for version, row in K.LONGHORN_CHARTS.items():
            self.assertRegex(version, r"^v\d+\.\d+\.\d+$")
            self.assertRegex(row["kubernetes_min"], MINOR)
            self.assertIsInstance(row["restrict_internal_traffic"], bool)
        for kubevirt, row in K.KUBEVIRT_KUBERNETES["rows"].items():
            self.assertRegex(kubevirt, MINOR)
            for kube in row["supported"] + row["eol"]:
                self.assertRegex(kube, MINOR)

    def test_no_kubernetes_minor_is_both_supported_and_end_of_life(self):
        for kubevirt, row in K.KUBEVIRT_KUBERNETES["rows"].items():
            self.assertFalse(set(row["supported"]) & set(row["eol"]), kubevirt)

    def test_every_listed_minor_is_inside_the_matrix_columns(self):
        low, high = (minor(v) for v in K.KUBEVIRT_KUBERNETES["columns"])
        for kubevirt, row in K.KUBEVIRT_KUBERNETES["rows"].items():
            for kube in row["supported"] + row["eol"]:
                self.assertTrue(low <= minor(kube) <= high, f"{kubevirt}: {kube}")

    def test_kubevirt_1_9_does_not_list_kubernetes_1_37(self):
        # Verified against kubevirt/sig-release f3d8a41e on 2026-10-23.
        self.assertEqual(["1.34", "1.35", "1.36"], sorted(K.KUBEVIRT_KUBERNETES["rows"]["1.9"]["supported"]))
        self.assertEqual("1.36", K.KUBEVIRT_KUBERNETES["columns"][1])

    def test_longhorn_turns_restrict_internal_traffic_on_in_1_12_1(self):
        # #793/#794: on by default in chart 1.12.1 and 1.13.0, absent in 1.12.0.
        self.assertFalse(K.LONGHORN_CHARTS["v1.12.0"]["restrict_internal_traffic"])
        self.assertTrue(K.LONGHORN_CHARTS["v1.12.1"]["restrict_internal_traffic"])
        self.assertTrue(K.LONGHORN_CHARTS["v1.13.0"]["restrict_internal_traffic"])
        self.assertEqual("1.34", K.LONGHORN_CHARTS["v1.13.0"]["kubernetes_min"])


if __name__ == "__main__":
    unittest.main()
