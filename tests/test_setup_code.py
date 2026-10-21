"""The one-time setup code that guards first-admin setup (#737).

Runs on the shared fake API (tests/fakes/kube.py) and a temporary data volume."""
import base64
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_auth as AUTH
import homestead_kube as K
import homestead_self as SELF
import homestead_setup_code as CODE
import homestead_shared as SHARED
from fakes.kube import Kube

NS = "lab"
SECRET_PATH = f"/api/v1/namespaces/{NS}/secrets/homestead-setup"
ACCOUNTS_PATH = f"/api/v1/namespaces/{NS}/secrets/homestead-auth"


def secret(code, name="homestead-setup"):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": NS},
            "data": {"code": base64.b64encode(code.encode()).decode()}}


def accounts(users):
    store = {"users": users, "signing_key": "k" * 40}
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "homestead-auth", "namespace": NS},
            "data": {"store.json": base64.b64encode(json.dumps(store).encode()).decode()}}


ADMIN = {"robin": {"salt": "c2FsdA==", "hash": "h", "ver": 1, "role": "admin"}}


class SetupCodeTests(unittest.TestCase):
    def setUp(self):
        AUTH._store_cache.update(at=0, data=None)
        AUTH._attempts.clear()
        CODE._said["hash"] = None
        saved = (AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME, AUTH.NS, SELF.NS, SHARED.DIR, AUTH.ITERATIONS)
        AUTH.kget, AUTH.ksend, AUTH.NS, SELF.NS = K.kget, K.ksend, NS, NS
        AUTH.SECRET_NAME = lambda: "homestead-auth"
        AUTH.ITERATIONS = 1000          # the password hash is not under test here
        self.data = self.enterContext(tempfile.TemporaryDirectory())
        SHARED.DIR = self.data
        self.slow = False

        def restore():
            (AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME, AUTH.NS, SELF.NS, SHARED.DIR, AUTH.ITERATIONS) = saved
            AUTH._store_cache.update(at=0, data=None)
            AUTH._attempts.clear()
        self.addCleanup(restore)

    def cluster(self, *objects):
        kube = self.enterContext(Kube(*objects))

        def slow_read(method, path, body):
            if self.slow:
                raise socket.timeout("timed out")
        kube.react("GET", ACCOUNTS_PATH, slow_read)
        return kube

    def stored_code(self, kube):
        obj = kube.object(SECRET_PATH)
        return obj and base64.b64decode(obj["data"]["code"]).decode()

    def stored_hash(self):
        try:
            with open(os.path.join(self.data, CODE.FILE), encoding="utf-8") as handle:
                return json.load(handle).get("hash")
        except FileNotFoundError:
            return None

    def setup(self, code, username="robin", password="a-long-password", addr="192.0.2.20"):
        body = {"username": username, "password": password}
        if code is not None:
            body["setup_code"] = code
        return CODE.first_admin(body, addr)

    # ------------------------------------------------------------ format
    def test_a_generated_code_is_ten_characters_grouped_like_the_pairing_code(self):
        code = CODE.generate()
        self.assertRegex(code, r"^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{2}$")
        self.assertNotEqual(code, CODE.generate())

    def test_typed_codes_are_forgiving_about_case_spaces_and_lookalikes(self):
        self.assertEqual("K7QF2M9XH0", CODE.normalise(" k7qf-2m9x-hO "))
        self.assertEqual(CODE.normalise("1111-1111-11"), CODE.normalise("ilIL-1111-11"))
        self.assertIsNone(CODE.normalise("K7QF-2M9X-H4T"))       # too short
        self.assertIsNone(CODE.normalise("K7QF-2M9X-H4TR"))      # too long
        self.assertIsNone(CODE.normalise("K7QF-2M9X-U4"))        # U is not in the alphabet
        self.assertIsNone(CODE.normalise(None))
        self.assertIsNone(CODE.normalise(12345))

    # ------------------------------------------------------------ start
    def test_a_fresh_install_makes_a_secret_and_keeps_only_the_hash(self):
        kube = self.cluster()
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            self.assertTrue(CODE.ensure())
        code = self.stored_code(kube)
        self.assertRegex(code, r"^\w{4}-\w{4}-\w{2}$")
        self.assertEqual(CODE.digest(code), self.stored_hash())
        saved = Path(self.data, CODE.FILE).read_text(encoding="utf-8")
        self.assertNotIn(code, saved)
        self.assertNotIn(CODE.normalise(code), saved)
        self.assertIn("lab/homestead-setup", log.getvalue())
        self.assertIn("kubectl -n lab get secret homestead-setup", log.getvalue())
        self.assertNotIn(code, log.getvalue())
        self.assertNotIn(CODE.normalise(code), log.getvalue())

    def test_a_restart_reuses_the_code_already_in_the_secret(self):
        kube = self.cluster(secret("K7QF-2M9X-H4"))
        self.assertTrue(CODE.ensure())
        self.assertEqual("K7QF-2M9X-H4", self.stored_code(kube))
        self.assertEqual(CODE.digest("K7QF-2M9X-H4"), self.stored_hash())
        self.assertFalse([w for w in kube.writes if w[0] in ("POST", "PUT")])

    def test_the_second_replica_reads_back_the_code_the_first_one_made(self):
        kube = self.cluster()

        def first_replica_won(method, path, body):
            kube.add(secret("ABCD-EFGH-JK"))
        kube.react("POST", f"/api/v1/namespaces/{NS}/secrets", first_replica_won)
        self.assertTrue(CODE.ensure())
        self.assertEqual("ABCD-EFGH-JK", self.stored_code(kube))
        self.assertEqual(CODE.digest("ABCD-EFGH-JK"), self.stored_hash())

    def test_a_code_that_is_not_valid_closes_setup_and_is_not_replaced(self):
        kube = self.cluster(secret("password"))
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            self.assertFalse(CODE.ensure())
        self.assertEqual("password", self.stored_code(kube))
        self.assertFalse(self.stored_hash())
        self.assertIn("does not hold a valid setup code", log.getvalue())
        self.assertNotIn("password", log.getvalue().replace("homestead-setup", ""))
        with self.assertRaises(PermissionError) as refused:
            self.setup("password")
        self.assertIn("does not hold a valid setup code", str(refused.exception))

    def test_an_install_with_accounts_removes_the_secret_and_the_hash(self):
        kube = self.cluster(accounts(ADMIN), secret("ABCD-EFGH-JK"))
        SHARED.write_json(os.path.join(self.data, CODE.FILE), {"hash": CODE.digest("ABCD-EFGH-JK")})
        self.assertFalse(CODE.ensure())
        self.assertIsNone(kube.object(SECRET_PATH))
        self.assertFalse(self.stored_hash())
        self.assertFalse([w for w in kube.writes if w[0] == "POST"])

    def test_an_install_with_accounts_and_nothing_left_writes_nothing(self):
        kube = self.cluster(accounts(ADMIN))
        self.assertFalse(CODE.ensure())
        self.assertFalse(os.path.exists(os.path.join(self.data, CODE.FILE)))
        self.assertFalse([w for w in kube.writes if w[0] in ("POST", "PUT")])

    def test_unreadable_accounts_create_and_delete_nothing(self):
        kube = self.cluster(secret("ABCD-EFGH-JK"))
        self.slow = True
        with self.assertRaises(AUTH.StoreUnavailable):
            CODE.ensure()
        self.assertEqual("ABCD-EFGH-JK", self.stored_code(kube))
        self.assertEqual([], [w for w in kube.writes if w[0] != "GET"])

    def test_the_start_loop_retries_until_the_cluster_answers(self):
        self.cluster()
        answers = [AUTH.StoreUnavailable("slow"), True]
        waits = []

        def ensure():
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch.object(CODE, "ensure", ensure), contextlib.redirect_stdout(io.StringIO()):
            CODE.run(sleep=waits.append)
        self.assertEqual([CODE.RETRY], waits)
        self.assertEqual([], answers)

    # ------------------------------------------------------------ setup
    def test_setup_without_the_code_is_refused(self):
        kube = self.cluster()
        CODE.ensure()
        for given in (None, "", "WRONG-CODE-00"):
            with self.subTest(given=given), self.assertRaises(PermissionError) as refused:
                self.setup(given)
            self.assertIn("setup code is not right", str(refused.exception))
        self.assertTrue(AUTH.needs_setup())
        self.assertIsNotNone(kube.object(SECRET_PATH))

    def test_the_right_code_makes_the_first_admin_and_then_stops_working(self):
        kube = self.cluster()
        CODE.ensure()
        code = self.stored_code(kube)
        self.assertEqual("robin", self.setup(code.lower().replace("-", " ")))
        self.assertEqual("admin", AUTH.role_of("robin"))
        self.assertIsNone(kube.object(SECRET_PATH))
        self.assertFalse(self.stored_hash())
        with self.assertRaises(PermissionError):
            self.setup(code, username="second")
        self.assertEqual(["robin"], [u["name"] for u in AUTH.list_users()])

    def test_a_preset_applied_after_start_replaces_the_generated_code(self):
        kube = self.cluster()
        CODE.ensure()
        generated = self.stored_code(kube)
        kube.add(secret("PRES-ET00-AB"))          # the installer's kubectl apply
        with self.assertRaises(PermissionError):
            self.setup(generated)
        self.assertEqual("robin", self.setup("PRES-ET00-AB"))

    def test_setup_makes_the_code_when_the_start_has_not_yet(self):
        kube = self.cluster()
        with self.assertRaises(PermissionError):
            self.setup("ABCD-EFGH-JK")
        self.assertIsNotNone(self.stored_code(kube))

    def test_a_bad_password_with_the_right_code_keeps_the_code(self):
        kube = self.cluster()
        CODE.ensure()
        code = self.stored_code(kube)
        with self.assertRaises(ValueError):
            self.setup(code, password="short")
        self.assertEqual(code, self.stored_code(kube))
        self.assertEqual("robin", self.setup(code))

    def test_wrong_codes_share_the_sign_in_limit_across_addresses(self):
        kube = self.cluster()
        CODE.ensure()
        code = self.stored_code(kube)
        for n in range(AUTH.MAX_USER_ATTEMPTS):
            with self.assertRaises(PermissionError):
                self.setup("WRONG-CODE-00", addr=f"192.0.2.{n % 3 + 1}")
        with self.assertRaises(PermissionError) as refused:
            self.setup(code, addr="198.51.100.7")
        self.assertIn("too many attempts", str(refused.exception))
        self.assertTrue(AUTH.needs_setup())

    def test_one_address_is_limited_like_sign_in(self):
        kube = self.cluster()
        CODE.ensure()
        code = self.stored_code(kube)
        for _ in range(AUTH.MAX_ATTEMPTS):
            with self.assertRaises(PermissionError):
                self.setup("WRONG-CODE-00")
        with self.assertRaises(PermissionError) as refused:
            self.setup(code)
        self.assertIn("too many attempts", str(refused.exception))

    def test_the_comparison_is_constant_time(self):
        kube = self.cluster()
        CODE.ensure()
        with mock.patch.object(CODE.hmac, "compare_digest", wraps=CODE.hmac.compare_digest) as compare:
            self.setup(self.stored_code(kube))
        compare.assert_called()


if __name__ == "__main__":
    unittest.main()
