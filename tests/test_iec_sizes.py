"""Sizes are IEC (#502): the server divides by 1024, as Kubernetes' 10Gi and
Longhorn do, so what it says to a person says KiB, MiB, GiB or TiB."""
import re
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server"
# A decimal unit word after a number or a {…} in a string a person reads.
DECIMAL = re.compile(r"(\d|\})\s?(KB|MB|GB|TB)(?![A-Za-z_])|MB/s")
# Home Assistant keeps each sensor's unit in its statistics; changing it
# would break the history people already have, so the MQTT sensor keeps it.
ALLOWED = {("homestead_mqtt.py", '("memgb", "RAM Used", "mem_gb", "GB", "mdi:memory"),')}


class IecSizes(unittest.TestCase):
    def test_no_message_labels_a_size_in_decimal_units(self):
        left = []
        for path in sorted(SERVER.glob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if '"""' in line or line.lstrip().startswith("#") or (path.name, line.strip()) in ALLOWED:
                    continue
                strings = re.findall(r'"[^"]*"|\'[^\']*\'', line.split("  #", 1)[0])
                if any(DECIMAL.search(text) for text in strings):
                    left.append(f"{path.name}:{number}")
        self.assertEqual(left, [], "say KiB, MiB, GiB or TiB")


if __name__ == "__main__":
    unittest.main()
