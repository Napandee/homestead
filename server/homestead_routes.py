"""Routes a feature module declares for itself (#363).

server.py routes requests and each feature lives in its own module
(CONTRIBUTING.md). Most of server.py's routing was one-line hand-offs -
`if p == "/api/firewall/save": return self._send(200, FIREWALL.save(b))` -
with the route's role declared again, apart, in homestead_route_policy.py.

A module can instead declare its routes, each with its role, beside the code
they call:

    ROUTES = {
        ("GET", "/api/firewall"): ("viewer", lambda request: inventory()),
        ("POST", "/api/firewall/save"): ("admin", lambda request: save(request.body)),
    }

The handler takes a Request and returns what is sent back as JSON, with 200,
or a Raw(body, ctype) for anything that is not JSON - an image, say - or a
Stream for a download too large to hold, or a Reply(code, body) for an
answer that is not a 200 - a 409 that carries the plan it refused, say. An
exception it raises is answered as one from server.py's own branches is.
server.py looks a request up here after its guard has checked who may make
it, before its own branches. homestead_route_policy.role() reads the same
table, so a route and its role are written once.

A route whose answer server.py keeps for a few seconds asks for it through
`cached(key, seconds, fn)` here, which is server.py's own cache once bound:
the same keys, so what server.py drops from it when something changes (the
network picture after a VIP repair, say) is dropped for the route too. A
route that changes something drops what it made stale with `forget(*keys)`,
or every key that starts with a prefix with `forget_prefix(prefix)`.

A route that needs the connection reads `request.conn` (`connected(request)`
refuses a call made without one): its headers, without Cookie, Authorization,
Proxy-Authorization, Cf-Access-Jwt-Assertion and the fleet signature; Homestead's
own cookies; whether it came over TLS or through Cloudflare; and who the guard
found - the Access sign-in, the linked Homestead, the API key - with `who()` for
the session. A Reply sets Homestead's own cookies (`cookies=(Cookie(...),)`) and
headers the server does not own (`headers=(("Location", "/"),)`), checked before
any is written: one that is refused (`Refused`) is answered 500, with none of them.
A path ending "/*" is a prefix route: it answers every path under it.

A module is listed in MODULES to be read. A route stays in server.py while it
needs the handler itself: a WebSocket, /mcp, a file served with its own headers.
"""
import collections
import collections.abc
import importlib
import re
import types
import homestead_cache as CACHE

MODULES = (
    "homestead_addons",
    "homestead_agent_audit",
    "homestead_agent_requests",
    "homestead_alerts",
    "homestead_allocation_probe",
    "homestead_api_keys",
    "homestead_app_settings",
    "homestead_auth",
    "homestead_baseline",
    "homestead_capacity",
    "homestead_changes",
    "homestead_cluster",
    "homestead_cluster_server",
    "homestead_cluster_shutdown",
    "homestead_components",
    "homestead_config_backup",
    "homestead_container_diagnostics",
    "homestead_container_rebalance",
    "homestead_deploy",
    "homestead_deploy_server",
    "homestead_diagnostics",
    "homestead_disk_v2",
    "homestead_disks",
    "homestead_files",
    "homestead_firewall",
    "homestead_fleet",
    "homestead_flow",
    "homestead_forecast",
    "homestead_group_tiers",
    "homestead_hardware",
    "homestead_helm",
    "homestead_history",
    "homestead_host_bonds",
    "homestead_host_bridge",
    "homestead_host_console",
    "homestead_host_os",
    "homestead_housekeeping",
    "homestead_icons",
    "homestead_image_cleanup",
    "homestead_imports",
    "homestead_ipam",
    "homestead_isos",
    "homestead_lhcapacity",
    "homestead_lhrebuild",
    "homestead_lhv2_setup",
    "homestead_lhv2_upgrade",
    "homestead_lifecycle",
    "homestead_longhorn",
    "homestead_move",
    "homestead_move_engine",
    "homestead_move_source",
    "homestead_mqtt",
    "homestead_namespaces",
    "homestead_network_server",
    "homestead_networking",
    "homestead_nodes",
    "homestead_nodeshell",
    "homestead_objectstore",
    "homestead_operations",
    "homestead_os_rollout",
    "homestead_outage",
    "homestead_overview_server",
    "homestead_passthrough",
    "homestead_place",
    "homestead_placement_mode",
    "homestead_platform",
    "homestead_portal",
    "homestead_power_jobs",
    "homestead_priority",
    "homestead_promote",
    "homestead_probe",
    "homestead_push",
    "homestead_rebalance",
    "homestead_reclass",
    "homestead_requests",
    "homestead_resources",
    "homestead_restore_test",
    "homestead_revert",
    "homestead_schedules",
    "homestead_self",
    "homestead_self_address",
    "homestead_self_data_server",
    "homestead_self_health",
    "homestead_set_restart",
    "homestead_settings_helpers",
    "homestead_setup",
    "homestead_share_servers",
    "homestead_shares",
    "homestead_signins",
    "homestead_snapshot_delete",
    "homestead_storage",
    "homestead_storage_classes",
    "homestead_storage_server",
    "homestead_unraid_vms",
    "homestead_updates",
    "homestead_upgrades",
    "homestead_uplinks",
    "homestead_uptime",
    "homestead_vm_review",
    "homestead_vm_server",
    "homestead_vms",
    "homestead_vmstore",
    "homestead_volumes",
    "homestead_workloads",
)
ROLES = ("viewer", "operator", "admin")

# addr: the address the request came from, as server.py sees it; conn: the
# Connection it came on, or None when a route is called in-process.
Request = collections.namedtuple("Request", "method path query body user role addr conn", defaults=("", None))
# What a route may know of the connection. headers: a Headers; cookies: Homestead's
# own, by name; tls, cloudflare: how it reached us; access: the Cloudflare Access
# claims; fleet: the linked Homestead that sent it; key: the API key it carried;
# who: a call that answers who is signed in (it may read the account store).
Connection = collections.namedtuple("Connection", "headers cookies tls cloudflare access fleet key who")
Route = collections.namedtuple("Route", "method path role handler module")
Raw = collections.namedtuple("Raw", "body ctype")
# A download sent as it is read: chunks (an iterator of bytes), its type, the
# name it saves as, and its size when known.
Stream = collections.namedtuple("Stream", "chunks ctype filename size")
# An answer with its own status: a refusal that carries more than its words,
# or a 202 for work that carries on. JSON unless told otherwise.
# headers: (name, value) pairs; cookies: Cookies. Both are checked before sending.
Reply = collections.namedtuple("Reply", "code body ctype headers cookies", defaults=("application/json", (), ()))
# max_age None: a cookie that lasts as long as the browser keeps it.
Cookie = collections.namedtuple("Cookie", "name value path max_age", defaults=("/", None))
# The only cookies a route may read or set.
COOKIES = ("homestead_session", "homestead_cluster", "homestead-data-move")
# Headers the server writes itself; a route may not set or weaken them.
OWNED_HEADERS = ("set-cookie", "content-type", "content-length", "connection", "content-security-policy",
                 "strict-transport-security", "x-frame-options", "x-content-type-options",
                 "referrer-policy", "permissions-policy", "x-homestead-version", "cache-control")
# Request headers a route never sees: they carry a secret, or the fleet
# signature that conn.fleet already checked.
SECRET_HEADERS = ("cookie", "authorization", "proxy-authorization", "cf-access-jwt-assertion", "x-homestead-fleet")
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_VALUE = re.compile(r"[\t\x20-\x7e\x80-\xff]*")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*")
_COOKIE_PATH = re.compile(r"/[\x21-\x3a\x3c-\x7e]*")
# A path on this site: never //host or /\host, which a browser reads as another site.
_LOCATION = re.compile(r"/(?![/\\])[^\s\\]*")


class Refused(Exception):
    """A reply a route may not send: Homestead's own mistake, answered 500."""


class Headers(collections.abc.Mapping):
    """The request's headers, read-only, by any case, without SECRET_HEADERS.
    A header sent twice is read as its first value."""

    def __init__(self, pairs):
        self._pairs = [(str(name), str(value)) for name, value in pairs if not _secret(str(name))]
        self._first = {}
        for name, value in self._pairs:
            self._first.setdefault(name.lower(), (name, value))

    def __getitem__(self, name):
        return self._first[str(name).lower()][1]

    def __iter__(self):
        return (name for name, _ in self._first.values())

    def __len__(self):
        return len(self._first)

    def __repr__(self):
        return f"Headers({self._pairs!r})"


def _secret(name):
    name = name.lower()
    return name in SECRET_HEADERS or name.startswith("x-homestead-fleet-")


def connected(request):
    """The request's Connection; a route that needs one is refused in-process."""
    if request.conn is None:
        raise PermissionError(f"{request.path} answers only over HTTP")
    return request.conn


def cookie_header(cookie, tls):
    """The Set-Cookie value for one of Homestead's cookies, as server.py writes
    its session cookie: Secure only behind TLS."""
    name, value, path, max_age = cookie
    if name not in COOKIES:
        raise Refused(f"a route may set only Homestead's own cookies, not {name!r}")
    if not isinstance(value, str) or not _COOKIE_VALUE.fullmatch(value):
        raise Refused(f"cookie {name} has a value a cookie cannot carry")
    if not isinstance(path, str) or not _COOKIE_PATH.fullmatch(path):
        raise Refused(f"cookie {name} has a path a cookie cannot carry")
    if max_age is not None and (type(max_age) is not int or max_age < 0):
        raise Refused(f"cookie {name} has a Max-Age that is not a whole number of seconds")
    return (f"{name}={value}; Path={path}; HttpOnly; SameSite=Strict{'; Secure' if tls else ''}"
            f"{f'; Max-Age={max_age}' if max_age is not None else ''}")


def check_header(name, value):
    """(name, value) when a route may send it; Refused otherwise."""
    if not isinstance(name, str) or not _TOKEN.fullmatch(name):
        raise Refused(f"{name!r} is not a header name")
    if name.lower() in OWNED_HEADERS:
        raise Refused(f"{name} is written by the server, not by a route")
    if not isinstance(value, str) or not _VALUE.fullmatch(value):
        raise Refused(f"{name} has a value a header cannot carry")
    if name.lower() == "location" and not _LOCATION.fullmatch(value):
        raise Refused("Location must be a path on this site")
    return name, value


def _tls(handler):
    try:
        return handler._over_tls()
    except AttributeError:      # a handler with no socket: the proxy's word alone
        return False


def _headers(handler):
    return getattr(handler, "headers", None) or {}


def connection(handler):
    """The Connection a route sees, from server.py's handler after its guard."""
    raw = _headers(handler)
    if raw.get("X-Homestead-Fleet") and getattr(handler, "_fleet_who", None) is False:
        handler._fleet_identity()       # a public path: the guard did not check the sender
    cookies = {name: value for name, value in handler._cookies().items() if name in COOKIES}
    return Connection(Headers(raw.items()), types.MappingProxyType(cookies), _tls(handler),
                      handler._via_cloudflare(), getattr(handler, "_access", None),
                      getattr(handler, "_fleet_from", None), getattr(handler, "api_key", None), handler._who)


def answer(handler, answer):
    """Send what a route returned. A Reply's cookies and headers are all checked
    before any is added, after what the guard already set (a refreshed session)."""
    if isinstance(answer, Stream):
        handler._send_stream(answer)
    elif isinstance(answer, Raw):
        handler._extra_headers += [("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")]
        handler._send(200, answer.body, answer.ctype)
    elif isinstance(answer, Reply):
        tls = _tls(handler) if answer.cookies else False
        lines = ([("Set-Cookie", cookie_header(Cookie(*cookie), tls)) for cookie in answer.cookies] +
                 [check_header(*header) for header in answer.headers])
        handler._extra_headers += lines
        handler._send(answer.code, answer.body, answer.ctype)
    else:
        handler._send(200, answer)

_table = {}
# How long the pages' main reads (overview, hosts, apps, VMs, volumes,
# storage) are kept for the UI: longer than its 15 s refresh, so a refresh can
# be served from the last one (#705). A write Homestead makes to what they show
# drops them at once (homestead_kube.fresh_reads).
PAGE_SECONDS = 20
_cached = [lambda key, seconds, fn: fn()]
_store = [{}]


def bind(cached, store=None):
    """server.py's cache, for routes whose answer it keeps a few seconds,
    and the dict it keeps them in, for routes that make one stale."""
    _cached[0] = cached
    if store is not None:
        _store[0] = store


def forget(*keys):
    """Drop these keys from server.py's cache, and from a read of them that
    is still running, so it is not kept after the change (#705)."""
    for key in keys:
        _store[0].pop(key, None)
    CACHE.forget_where(lambda key: key in keys)


def forget_prefix(prefix):
    """Drop every key from server.py's cache that starts with prefix."""
    for key in [k for k in _store[0] if k.startswith(prefix)]:
        _store[0].pop(key, None)
    CACHE.forget_where(lambda key: key.startswith(prefix))


def cached(key, seconds, fn):
    """fn(), or what it returned less than seconds ago under key."""
    return _cached[0](key, seconds, fn)


def table():
    """Every declared route, by (method, path). Read once; a module that
    declares a route twice, or one another module declares, is refused."""
    if _table:
        return _table
    found = {}
    for name in MODULES:
        module = importlib.import_module(name)
        for (method, path), (role, handler) in getattr(module, "ROUTES", {}).items():
            key = (method.upper(), path)
            if key in found:
                raise ValueError(f"{method} {path} is declared by {found[key].module} and {name}")
            if role not in ROLES:
                raise ValueError(f"{method} {path} in {name} has no known role: {role!r}")
            if not callable(handler):
                raise ValueError(f"{method} {path} in {name} has no handler")
            if "*" in path and not (path.endswith("/*") and path.count("*") == 1):
                raise ValueError(f"{method} {path} in {name}: a prefix route ends in /* and has no other *")
            found[key] = Route(key[0], path, role, handler, name)
    _table.update(found)
    return _table


def find(method, path):
    """The Route for a request, or None when server.py answers it itself: the
    exact path, or else the longest prefix route ("/api/x/*") above it."""
    method, routes = method.upper(), table()
    route = routes.get((method, path))
    if route is not None or "*" in path:
        return route
    stems = [p for m, p in routes if m == method and p.endswith("/*") and path.startswith(p[:-1])]
    return routes[(method, max(stems, key=len))] if stems else None


def role(path, method):
    """The role a declared route needs, or None when it is not one."""
    route = find(method, path)
    return route.role if route else None
