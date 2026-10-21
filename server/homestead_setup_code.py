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
* Homestead keeps only the code's hash on its data volume. Guesses go through
  the sign-in rate limit, and the comparison takes constant time.
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
import time
import urllib.error

import homestead_auth as AUTH
import homestead_kube as KUBE
import homestead_self as SELF
import homestead_shared as SHARED

SECRET = "homestead-setup"
FILE = "setup-code.json"
LENGTH = 10
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# Every attempt shares one per-account counter, whatever address it comes
# from; no username has a ':', so no account can share it.
LIMIT_KEY = "setup:code"
RETRY = 10
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


def digest(code):
    return hashlib.sha256(b"homestead-setup-code-v1\0" + normalise(code).encode()).hexdigest()


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


def _stored_hash():
    try:
        return SHARED.read_json(_file(), {}).get("hash") or ""
    except SHARED.Unreadable:
        return ""


def _write_hash(value):
    if value:
        SHARED.write_json(_file(), {"hash": value, "secret": where()})
    elif os.path.exists(_file()) and _stored_hash():
        SHARED.write_json(_file(), {})


_said = {"hash": None}


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
    if code is None:
        code = _create()
    if normalise(code) is None:
        _write_hash("")
        if _said["hash"] != "invalid":
            _say(f"Setup code: the Secret {where()} does not hold a valid setup code "
                 f"({LENGTH} characters from {ALPHABET}). Setup stays closed until it does; "
                 f"delete the Secret and restart Homestead to make a new one.")
            _said["hash"] = "invalid"
        return False
    value = digest(code)
    _write_hash(value)
    if _said["hash"] != value:
        _say(f"Setup code: in the Secret {where()}. Show it with: {show_command()}")
        _said["hash"] = value
    # The first admin may have been made elsewhere while this one created.
    if not AUTH.needs_setup():
        finish()
        return False
    return True


def finish():
    """The first admin exists: the code is spent."""
    try:
        ksend("DELETE", _path())
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    _write_hash("")
    _said["hash"] = None


def first_admin(body, addr):
    """Make the first administrator, if the setup code is right. Returns the
    username. Wrong codes count as failed sign-ins (AUTH's rate limit)."""
    reservation = AUTH._reserve_attempt(LIMIT_KEY, addr)
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
    given = normalise(body.get("setup_code"))
    expected = _stored_hash()
    # Compared whatever was typed, so a malformed code takes as long as a wrong one.
    if not hmac.compare_digest(digest(given) if given else "-", expected or "--") or not given:
        raise PermissionError("That setup code is not right. It is in the Secret "
                              f"{where()}; show it with: {show_command()}")
    AUTH._release_attempt(reservation)
    AUTH.create_user(body.get("username"), body.get("password"), first_only=True)
    finish()
    return (body.get("username") or "").strip().lower()


def run(sleep=time.sleep):
    """At start: make the code, or tidy it away, retrying until the cluster answers."""
    while True:
        try:
            ensure()
            return
        except Exception as error:
            _say(f"Setup code: not ready yet ({AUTH.unavailable_cause(error)}); trying again")
            sleep(RETRY)
