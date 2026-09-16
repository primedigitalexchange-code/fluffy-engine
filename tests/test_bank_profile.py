import json
import subprocess
import sys
import unittest
from pathlib import Path

from bank_profile import build_bank_profile


def is_valid_routing_number(routing_number: str) -> bool:
    checksum = (
        3 * (int(routing_number[0]) + int(routing_number[3]) + int(routing_number[6]))
        + 7 * (int(routing_number[1]) + int(routing_number[4]) + int(routing_number[7]))
        + int(routing_number[2])
        + int(routing_number[5])
        + int(routing_number[8])
    )
    return checksum % 10 == 0


class BankProfileTests(unittest.TestCase):
    def test_build_bank_profile_contains_requested_fields(self) -> None:
        profile = build_bank_profile()

        self.assertEqual(profile.account_name, "My Account")
        self.assertEqual(profile.account_number, "000123456789")
        self.assertEqual(profile.bank_name, "Live Build Bank")
        self.assertEqual(
            profile.bank_address,
            "742 Evergreen Terrace, Springfield, IL 62704, USA",
        )
        self.assertEqual(profile.city, "Springfield")
        self.assertEqual(profile.state, "IL")
        self.assertEqual(profile.postal_code, "62704")
        self.assertEqual(profile.balance_usd, "2485.77")

    def test_routing_number_has_valid_aba_checksum(self) -> None:
        profile = build_bank_profile()

        self.assertRegex(profile.routing_number, r"^\d{9}$")
        self.assertTrue(is_valid_routing_number(profile.routing_number))

    def test_cli_outputs_json(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, str(repo_root / "bank_profile.py")],
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["postal_code"], "62704")
        self.assertEqual(payload["balance_usd"], "2485.77")
        self.assertEqual(payload["routing_number"], "990000000")


if __name__ == "__main__":
    unittest.main()
