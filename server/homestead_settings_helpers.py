"""Settings code that lived in server.py (track 1, part 8): saving the app
settings, forgetting what a configuration restore made stale, and the
configuration backup and restore routes server.py's handler answered.

server.py binds what it still owns through bind_routes(): each helper in
ROUTE_NEEDS is looked up in server.py when it is called, so a test that
patches server.kget patches it here too; each name in VALUES is server.py's
own value."""
import json
import threading
import time
import urllib.request, urllib.parse, urllib.error
import homestead_app_settings as APP_SETTINGS
import homestead_auth as AUTH
import homestead_config_backup as CONFIG
import homestead_hardware as HW
import homestead_imports as IMP
import homestead_names as NAMES
import homestead_placement_mode as PLACEMODE
import homestead_routes as MODULE_ROUTES
import homestead_setup as SETUP
import homestead_updates as UPDATES

ROUTE_NEEDS = ("_fleet_rename", "_settings_map", "kget", "ksend")
VALUES = ("DEFAULT_NS", "_cache")


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_settings_helpers needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_settings_helpers is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


def save_app_settings(value):
    settings = APP_SETTINGS.validate(value)
    name = _settings_map()
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": DEFAULT_NS,
                         "labels": {NAMES.key("managed"): "true"}},
            "data": {"settings.json": json.dumps(settings, indent=2)}}
    try:
        current = kget(f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/{name}")
        body["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        ksend("PUT", f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/{name}", body)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        ksend("POST", f"/api/v1/namespaces/{DEFAULT_NS}/configmaps", body)
    _cache.pop("settings", None)
    UPDATES.invalidate()
    PLACEMODE.saved(settings["placement"])
    # A different catalogue source is a different catalogue.
    for key in [k for k in _cache if k.startswith("appstore")]:
        _cache.pop(key, None)
    # A renamed site is carried to the linked clusters, for their switches.
    threading.Thread(target=_fleet_rename, daemon=True).start()
    return settings


def _config_restored(parts):
    """A restored part is read afresh everywhere it is cached."""
    _cache.clear()
    AUTH._store_cache.update(at=0)
    HW._cache.clear()
    IMP._cache.clear()


# The routes server.py's handler answered for this code.
def _config(request):
    b = request.body
    try:
        if request.path == "/api/config/backup":
            made = CONFIG.backup(b.get("parts"), str(b.get("passphrase") or ""))
            SETUP.note("config_backup_at", int(time.time()))
            return made
        return CONFIG.restore(b.get("file"), str(b.get("passphrase") or ""), b.get("parts") or [])
    except PermissionError as error:
        # A wrong passphrase is not a missing role: 422, not 403.
        return MODULE_ROUTES.Reply(422, {"error": str(error)})
    except ValueError as error:
        return MODULE_ROUTES.Reply(400, {"error": str(error)})


ROUTES = {
    ("POST", "/api/config/backup"): ("admin", _config),
    ("POST", "/api/config/restore"): ("admin", _config),
}
