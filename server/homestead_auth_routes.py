"""Signing in and out, and a person's own session (#764).

Setup, sign-in, sign-out, the password change and the dashboard layout, as
routes (homestead_routes.py). They need the connection: a session cookie is
set or cleared, setup is refused through the tunnel, and every sign-in history
line says where it came from. server.py's guard still checks the CSRF header,
the JSON type and the origin of setup and sign-in before they get here, and
refreshes a stale session; its cookie comes before the route's own.
"""
import homestead_auth as AUTH
import homestead_routes as MODULE_ROUTES
import homestead_setup_code as SETUP_CODE
import homestead_signins as SIGNINS

Reply, Cookie, connected = MODULE_ROUTES.Reply, MODULE_ROUTES.Cookie, MODULE_ROUTES.connected
_default_ns = [None]


def bind(default_ns):
    """Where Deploy puts a new app unless told otherwise, for the state."""
    _default_ns[0] = default_ns


def signin(request, event, user, ok=True, detail=""):
    """One line in the sign-in history, with where it came from."""
    conn = connected(request)
    SIGNINS.record(event, user, request.addr, ok, detail, conn.headers.get("User-Agent", ""),
                   "Cloudflare" if conn.cloudflare else "")


def session(token, max_age):
    return Cookie(AUTH.COOKIE, token, max_age=max_age)


SIGNED_OUT = Cookie(AUTH.COOKIE, "", max_age=0)


def state(request):
    who = connected(request).who()
    return {"setup": AUTH.needs_setup() and not (who and who.get("fleet")),
            "via": ((who or {}).get("fleet") or {}).get("name", ""),
            "user": who["user"] if who else None,
            "role": who["role"] if who else None,
            "remember": bool(who and who.get("remember")),
            "session_expires": who.get("expires") if who else None,
            "session_started": who.get("started") if who else None,
            "session_max_days": AUTH.ABSOLUTE_TTL // 86400,
            # Where Deploy puts a new app unless told otherwise.
            "default_namespace": _default_ns[0] if who else None,
            "roles": list(AUTH.ROLES)}


def setup(request):
    # The first administrator: from the LAN only, and with the setup code (#737).
    b = request.body
    if connected(request).cloudflare:
        return Reply(403, {"error": "finish setting Homestead up from your LAN; "
                                    "setup is not offered through the tunnel"})
    first = SETUP_CODE.first_admin(b, request.addr, request.conn.headers.get("User-Agent", ""))
    signin(request, "setup", first, detail="the first administrator")
    remember = bool(b.get("remember"))
    return Reply(200, {"ok": True, "user": b.get("username")},
                 cookies=(session(AUTH.issue_token(first, remember), AUTH.idle_ttl(remember)),))


def login(request):
    connected(request)          # refused in-process before anything changes
    b = request.body
    tried = (b.get("username") or "").strip().lower()
    try:
        remember = bool(b.get("remember"))
        tok = AUTH.login(b.get("username"), b.get("password"), request.addr, remember)
    except PermissionError as e:
        blocked = "too many" in str(e)
        signin(request, "signin-blocked" if blocked else "signin-failed", tried, ok=False, detail=str(e))
        return Reply(401, {"error": str(e)})
    signin(request, "signin", tried, detail="kept signed in" if remember else "")
    return Reply(200, {"ok": True, "remember": remember, "user": (b.get("username") or "").strip().lower()},
                 cookies=(session(tok, AUTH.idle_ttl(remember)),))


def logout(request):
    signin(request, "signout", request.user)
    return Reply(200, {"ok": True}, cookies=(SIGNED_OUT,))


def save_preferences(request):
    try:
        return AUTH.save_dashboard_preferences(request.user, request.body)
    except AUTH.StoreConflict as error:
        return Reply(409, {"error": str(error)})


def password(request):
    connected(request)          # refused in-process before anything changes
    b = request.body
    try:
        AUTH.change_password(request.user, b.get("old"), b.get("new"))
    except (PermissionError, ValueError) as error:
        signin(request, "password-failed", request.user, ok=False, detail=str(error))
        raise
    signin(request, "password", request.user)
    return Reply(200, {"ok": True}, cookies=(session(AUTH.issue_token(request.user), AUTH.SESSION_TTL),))


def signout_everywhere(request):
    connected(request)          # refused in-process before anything changes
    AUTH.logout_everywhere(request.user)
    signin(request, "signout-everywhere", request.user)
    return Reply(200, {"ok": True}, cookies=(SIGNED_OUT,))


# Its routes and who may use them (homestead_routes.py). Setup and sign-in are
# public paths: the guard lets them through before it reads a session.
ROUTES = {
    ("GET", "/api/auth/state"): ("viewer", state),
    ("GET", "/api/auth/preferences/dashboard"): ("viewer", lambda request: AUTH.dashboard_preferences(request.user)),
    ("POST", "/api/auth/preferences/dashboard"): ("viewer", save_preferences),
    ("POST", "/api/auth/setup"): ("operator", setup),
    ("POST", "/api/auth/login"): ("operator", login),
    ("POST", "/api/auth/logout"): ("viewer", logout),
    ("POST", "/api/auth/password"): ("viewer", password),
    ("POST", "/api/auth/signout-everywhere"): ("viewer", signout_everywhere),
}
