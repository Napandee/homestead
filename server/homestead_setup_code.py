"""The one-time setup code that guards first-admin setup (#737).

Until the first account exists, whoever reaches Homestead first could make
themselves its administrator. So the first-admin form also asks for a setup
code that only someone with access to the cluster can read:

* The code lives in a Secret, homestead-setup, in Homestead's own namespace.
  The installer prints it beside the address, and the Helm notes give the one
  command that shows it. Homestead's log says only where it is, never the code.
* Each replica makes sure the Secret exists when it starts. The first one to
  create it wins; another gets AlreadyExists and reads it back, so every
  replica answers to the same code without a leader.
* Homestead keeps only the code's hash on its data volume: PBKDF2-HMAC-SHA256
  with a fresh random salt, as for passwords, so a copy of the volume does not
  give the code away to a table of all 2^50. Guesses go through the sign-in
  rate limit, and the comparison takes constant time.
* A code set in advance (the installer's HS_SETUP_CODE, the chart's setupCode)
  is that same Secret made before Homestead starts, or applied over the one it
  made: the Secret is read again on every attempt, so the latest one counts.
* Once the first admin exists the Secret is deleted and the hash cleared - at
  once by the replica that made the account, and at every start by any replica
  that finds accounts, which also tidies one a Helm upgrade puts back.

The format is #602's pairing code: Crockford base32, which leaves out I, L, O
and U, grouped for reading (ABCD-EFGH-JK); 10 characters, 50 bits.
"""
import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
import urllib.error

import homestead_auth as AUTH
import homestead_kube as KUBE
import homestead_self as SELF
import homestead_shared as SHARED
import homestead_signins as SIGNINS

SECRET = "homestead-setup"
FILE = "setup-code.json"
LENGTH = 10
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# Every attempt shares one per-account counter, whatever address it comes
# from; no username has a ':', so no account can share it.
LIMIT_KEY = "setup:code"
RETRY = 10
MAX_RETRY = 600
TIDY_TRIES = 6
_LOOKALIKES = str.maketrans({"O": "0", "I": "1", "L": "1"})


def kget(path):
    return KUBE.kget(path)


def ksend(method, path, body=None):
    return KUBE.ksend(method, path, body)


def generate():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalise(text):
    """The code as typed, made comparable; None if it cannot be a code."""
    if not isinstance(text, str):
        return None
    code = "".join(text.split()).replace("-", "").upper().translate(_LOOKALIKES)
    if len(code) != LENGTH or any(c not in ALPHABET for c in code):
        return None
    return code


def _derive(code, salt, iterations):
    return base64.b64encode(hashlib.pbkdf2_hmac(
        "sha256", b"homestead-setup-code-v1\0" + code.encode(), salt, iterations)).decode()


def hashed(code):
    """What the data volume keeps for code: a salted, slow hash."""
    salt = secrets.token_bytes(16)
    return {"salt": base64.b64encode(salt).decode(), "iterations": AUTH.ITERATIONS,
            "hash": _derive(normalise(code), salt, AUTH.ITERATIONS)}


def matches(code, record):
    """Whether code is the one record was made from, compared in constant time."""
    given = normalise(code)
    try:
        salt = base64.b64decode(record["salt"], validate=True)
        iterations, expected = int(record["iterations"]), str(record["hash"])
    except (KeyError, TypeError, ValueError):
        return False
    # Hashed whatever was typed, so a malformed code takes as long as a wrong one.
    calc = _derive(given or "-", salt, iterations)
    return hmac.compare_digest(calc, expected) and given is not None


def where():
    return f"{SELF.NS}/{SECRET}"


def show_command():
    return f"kubectl -n {SELF.NS} get secret {SECRET} -o jsonpath='{{.data.code}}' | base64 -d"


def _path():
    return f"/api/v1/namespaces/{SELF.NS}/secrets/{SECRET}"


def _file():
    return os.path.join(SHARED.DIR, FILE)


def _read_secret():
    """(secret, its code as stored) or (None, None) when there is none."""
    try:
        found = kget(_path())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None, None
        raise
    try:
        return found, base64.b64decode((found.get("data") or {}).get("code", "")).decode().strip()
    except (ValueError, UnicodeDecodeError):
        return found, ""


def _create():
    code = generate()
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": SECRET, "namespace": SELF.NS,
                         "labels": {"homestead.io/managed": "true"}},
            "data": {"code": base64.b64encode(code.encode()).decode()}}
    try:
        ksend("POST", f"/api/v1/namespaces/{SELF.NS}/secrets", body)
        return code
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise
    # Another replica made it first: its code is the one.
    return _read_secret()[1]


def _stored():
    try:
        return SHARED.read_json(_file(), {})
    except SHARED.Unreadable:
        return {}


def _write_hash(record):
    """Keep record (or none) as the live code's hash. The last spent code's
    hash stays beside it (see _spend)."""
    stored = _stored()
    value = {"spent": stored["spent"]} if stored.get("spent") else {}
    if record:
        value.update(record, secret=where())
    if value != stored:
        SHARED.write_json(_file(), value)


def _spend():
    """The live code made the first admin: keep its hash, and only that, as
    spent. A code once used never opens setup again - a setupCode left in Helm
    values comes back with every upgrade, and would otherwise be the way in if
    the accounts were ever emptied."""
    stored = _stored()
    if stored.get("hash"):
        SHARED.write_json(_file(), {"spent": {k: stored[k] for k in ("salt", "iterations", "hash") if k in stored}})


class TidyFailed(Exception):
    """The first admin exists, but the spent code's Secret could not be removed."""


_said = {"code": None}


def _say(text):
    print(text, flush=True)


def ensure():
    """While no account exists, make sure the Secret holds a valid code and
    the data volume its hash; True when setup can be finished. With accounts,
    tidy up instead and answer False. Accounts that cannot be read raise
    AUTH.StoreUnavailable: nothing is made or removed then."""
    if not AUTH.needs_setup():
        finish()
        return False
    _, code = _read_secret()
    if code is not None and _stored().get("spent") and matches(code, _stored()["spent"]):
        _say(f"Setup code: the code in the Secret {where()} was already used once; making a new one")
        _delete_secret()
        code = None
    if code is None:
        code = _create()
    if normalise(code) is None:
        _write_hash(None)
        if _said["code"] != "invalid":
            _say(f"Setup code: the Secret {where()} does not hold a valid setup code "
                 f"({LENGTH} characters from {ALPHABET}). Setup stays closed until it does; "
                 f"delete the Secret and restart Homestead to make a new one.")
            _said["code"] = "invalid"
        return False
    # Rehashed only when the code changed (a preset applied over it), so
    # replicas sharing the volume do not keep rewriting each other's salt.
    if not matches(code, _stored()):
        _write_hash(hashed(code))
    stored = _stored().get("hash")
    if _said["code"] != stored:
        _say(f"Setup code: in the Secret {where()}. Show it with: {show_command()}")
        _said["code"] = stored
    # The first admin may have been made elsewhere while this one created.
    if not AUTH.needs_setup():
        finish()
        return False
    return True


def _delete_secret():
    try:
        ksend("DELETE", _path())
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise


def finish():
    """The first admin exists: the code is spent and its Secret removed."""
    _spend()
    _said["code"] = None
    try:
        _delete_secret()
    except Exception as error:
        raise TidyFailed(AUTH.unavailable_cause(error)) from error


def _tidy_later():
    threading.Thread(target=run, name="setup-code-tidy", daemon=True).start()


def _refused(event, body, addr, agent, detail):
    """A refused setup attempt, in the sign-in history like a refused sign-in,
    so someone holding setup off with wrong codes can be seen. Never the code."""
    SIGNINS.record(event, (body.get("username") or "").strip().lower(), addr, False, detail=detail, agent=agent)


def first_admin(body, addr, agent=""):
    """Make the first administrator, if the setup code is right. Returns the
    username. Wrong codes count as failed sign-ins (AUTH's rate limit) and are
    recorded as such."""
    try:
        reservation = AUTH._reserve_attempt(LIMIT_KEY, addr)
    except PermissionError as error:
        _refused("signin-blocked", body, addr, agent, f"first-admin setup: {error}")
        raise
    try:
        ready = ensure()
    except BaseException:
        AUTH._release_attempt(reservation)
        raise
    if not ready and not AUTH.needs_setup():
        AUTH._release_attempt(reservation)
        raise PermissionError("setup has already been completed")
    if not ready:
        raise PermissionError(f"The Secret {where()} does not hold a valid setup code. "
                              "Delete it and restart Homestead to make a new one.")
    if not matches(body.get("setup_code"), _stored()):
        _refused("signin-failed", body, addr, agent, "first-admin setup: wrong setup code")
        raise PermissionError("That setup code is not right. It is in the Secret "
                              f"{where()}; show it with: {show_command()}")
    AUTH._release_attempt(reservation)
    AUTH.create_user(body.get("username"), body.get("password"), first_only=True)
    # The account exists now, so this answers success whatever happens next:
    # a Secret that cannot be removed yet opens nothing, and is retried.
    try:
        finish()
    except Exception as error:
        _say(f"Setup code: the first administrator exists; the Secret {where()} could not be "
             f"removed yet ({AUTH.unavailable_cause(error)}) and will be removed in the background")
        _tidy_later()
    return (body.get("username") or "").strip().lower()


def run(sleep=time.sleep):
    """At start: make the code, or tidy it away. Retries with a growing wait
    until the cluster answers; a tidy-up that keeps failing (the accounts
    exist, so nothing depends on it) is given up after TIDY_TRIES."""
    wait, tidy_failures = RETRY, 0
    while True:
        try:
            ensure()
            return
        except TidyFailed as error:
            tidy_failures += 1
            if tidy_failures >= TIDY_TRIES:
                _say(f"Setup code: the Secret {where()} could not be removed ({error}); "
                     "Homestead tries again at its next start")
                return
            _say(f"Setup code: the Secret {where()} could not be removed yet ({error}); trying again")
        except Exception as error:
            _say(f"Setup code: not ready yet ({AUTH.unavailable_cause(error)}); trying again")
        sleep(wait)
        wait = min(wait * 2, MAX_RETRY)
