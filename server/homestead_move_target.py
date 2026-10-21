"""A move's backup target: both clusters' Longhorn must write to the same bucket.

The destination points its own Longhorn at the source's backup storage for the
length of a transfer (with a credentials Secret when the two differ) and puts
the previous setting back afterwards. One transfer holds it at a time; a
journal on Homestead's own volume says which, so a restart still gives it back.

Split out of homestead_move_engine (#623).
"""
import json
import os
import secrets
import urllib.error
import urllib.parse
import homestead_shared as SHARED
import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_transfer_cleanup as CLEANUP
import homestead_move_state as STATE


_target_lock = SHARED.SharedLock("move-backup-target", strict=True, directory=lambda: STATE.DATA_DIR, timeout=60)
TARGET_STORE = "move-backup-target.json"


def _target_journal(value=None):
    path = os.path.join(STATE.DATA_DIR, TARGET_STORE)
    if value is not None:
        SHARED.write_json(path, value, durable=True, mode=0o600)
        return value
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def _borrow_target(move, there=None):
    try:
        return _borrow_target_locked(move, there)
    except urllib.error.HTTPError as error:
        # Secret admission errors can include their submitted stringData.
        raise STATE.CLIENT.Unreachable("Could not prepare transfer backup storage; check Kubernetes and retry") from error


def _borrow_target_locked(move, there=None):
    """Journal a single cluster-wide lease before any Kubernetes mutation."""
    with _target_lock:
        journal = _target_journal()
        if journal and journal["owner"] != move["id"]:
            STATE._note(move, move.get("progress", 1), "Waiting for another transfer to release backup storage")
            return False
        here = STATE.LH.backup_target_state()
        if not journal:
            target = move.get("transfer_target")
            if target is None:
                there = there or STATE.CLIENT.remote(move["cluster"], "/api/move/target")
                credentials = there.get("credentials") or {}
                endpoint = there.get("endpoint") or credentials.get("AWS_ENDPOINTS", "")
                if endpoint and hasattr(STATE.CLIENT, "answers") and not STATE.CLIENT.answers(endpoint):
                    raise ValueError(f"this cluster cannot reach {move['cluster']}'s backup storage at {endpoint}. "
                                     f"Give it an address this cluster can reach - {move['cluster']}'s Migration button "
                                     "under Linked clusters - then retry")
                shared = STATE._same_target(STATE.LH.backup_target(), there)
                secret = "" if shared else f"{STATE.JOIN_SECRET}-{move['id']}-{secrets.token_hex(4)}"
                target = {"state": here if shared else STATE.LH.transfer_target_state(here, there["url"], secret, credentials),
                          "secret": secret, "credentials": credentials if secret else {}}
                move["transfer_target"] = target
                move["previous_target"] = STATE.LH.backup_target().get("url", "")
                STATE._store(move)
            journal = {"owner": move["id"], "previous": here, "applied": target["state"]}
            _target_journal(journal)
        if here == journal["applied"]:
            return True
        if here != journal["previous"]:
            raise ValueError("Backup storage changed during this transfer; cancel it and review the storage settings")
        target = move["transfer_target"]
        if target["secret"]:
            path = f"/api/v1/namespaces/{STATE.LHNS}/secrets"
            secret = target["secret"]
            existing = KUBE.get_optional(f"{path}/{secret}", read=STATE.kget)
            if existing and not STATE._ours(existing, move["id"]):
                raise ValueError("The transfer's backup credentials already exist and belong to another workload")
            if not existing:
                STATE.ksend("POST", path, {
                    "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                    "metadata": {"name": secret, "namespace": STATE.LHNS,
                                 "annotations": {NAMES.key(STATE.MOVE_ID): move["id"]},
                                 "labels": {NAMES.key("managed"): "true"}},
                    "stringData": {str(k): str(v) for k, v in target["credentials"].items()}})
        # Webhooks may echo submitted S3 keys. Keep their response private.
        try:
            applied = STATE.LH.replace_backup_target_state(here, journal["applied"])
        except Exception as error:
            raise STATE.CLIENT.Unreachable("Could not apply the transfer backup target; check backup storage and retry") from error
        if not applied:
            raise ValueError("Backup storage changed while the transfer was starting; cancel it and review the settings")
        return True


def _release_target(move):
    with _target_lock:
        journal = _target_journal()
        if journal and journal["owner"] == move["id"]:
            # A changed setting belongs to the admin; leave it alone.
            try:
                STATE.LH.replace_backup_target_state(journal["applied"], journal["previous"])
            except Exception as error:
                raise STATE.CLIENT.Unreachable("Could not restore previous backup storage; cleanup will retry") from error
            _target_journal({})
        move.setdefault("flags", {}).pop("target_cleanup_pending", None)
        target = move.get("transfer_target") or {}
        secret = target.get("secret")
        if secret:
            previous = (journal.get("previous") or {}).get("value") or {}
            baseline_secret = previous.get("credentialSecret") if isinstance(previous, dict) else ""
            current = STATE.LH.backup_target_state()
            current_secret = "" if current["harvester"] else current["value"].get("credentialSecret", "")
            if current_secret != secret and baseline_secret != secret:
                path = f"/api/v1/namespaces/{STATE.LHNS}/secrets/{secret}"
                existing = CLEANUP.optional(STATE.kget, path)
                if existing and STATE._ours(existing, move["id"]):
                    if not CLEANUP.delete(STATE.kget, STATE.ksend, path, existing):
                        raise STATE.CLIENT.Unreachable("Waiting for transfer backup credentials to be removed")
        if move.get("status") in ("succeeded", "cancelled"):
            move.pop("transfer_target", None)


def _cleanup_target(move):
    pending = "; Backup storage cleanup is pending; Homestead will retry automatically"
    try:
        _release_target(move)
        move["message"] = move.get("message", "").replace(pending, "")
    except Exception:
        move.setdefault("flags", {})["target_cleanup_pending"] = True
        if "Backup storage cleanup is pending" not in move.get("message", ""):
            move["message"] = move.get("message", "") + pending
