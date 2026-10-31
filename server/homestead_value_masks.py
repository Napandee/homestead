"""What a role below the one that may reveal a Secret is shown of values that
can carry a credential outside a Secret.

- An address (a "URL token": a scheme, "://" - or ":/" or ":\\", which
  other parsers accept - and everything up to the next ASCII whitespace)
  loses only what can be a credential: its login ("user:pass@" becomes
  "•••@"), and the value of a parameter a credential goes by ("?token=•••",
  in the query or the fragment). The rest stays as it is. Where the address
  stops being plain - a host that is not a host and port, a path or a
  parameter name with "%", ";", "=", "\\", a quote or a word a credential
  goes by - everything from there to the end of the token is hidden, so a
  client that reads the address another way still finds no credential.
- A text that is a JSON object or list is checked string by string, so an
  address inside it ends where its string ends (an annotation Homestead
  keeps as JSON round-trips); with a credential, it is written back compact.
- A text longer than MAX_TEXT that holds an address is hidden whole.
- A copy saved back with a masked text keeps what is stored there, when the
  mask is exactly what the stored text shows (restore()); any other masked
  text is refused, so the mask is never saved as a value.
- The Helm controller's install and delete pods run `helm` with the chart's
  values, and a chart may print them, so their logs go only to that role.

The role is the one GET /api/resources/reveal declares, read from the route
table so the two cannot drift apart. Everything here is linear in the
length of the text: anyone who can set an annotation or cause an event
writes it."""
import copy
import json
import re

HIDDEN = "•••"
REVEAL = ("GET", "/api/resources/reveal")
# ASCII whitespace: the only thing that ends a URL token.
SPACE = " \t\r\n\f\v"
SPACES = re.compile(r"[ \t\r\n\f\v]+")
# A URL token: a scheme of at most 32 characters (each start is tried in
# bounded time), ":" and one or two slashes or backslashes, then everything
# up to ASCII whitespace or the end of the text.
TOKEN = re.compile(r"(?i)[a-z][a-z0-9+.-]{0,31}:[\\/]{1,2}[^ \t\r\n\f\v]*")
# A word a credential goes by (tokens, keys, passwords, signatures,
# sessions; a harmless word hidden too is the safe side). A parameter is
# also hidden by "key" or "code" anywhere in its name.
SECRET_WORD = re.compile(r"pass|pwd|secret|token|sig|credential|auth|session|jwt|bearer|cookie|private|hmac|otp|"
                         r"ticket|apikey|api-key|api_key|access-key|access_key", re.I)
SECRET_PARAM = re.compile(SECRET_WORD.pattern + r"|key|code", re.I)
# The parts of an address shown as they are; anything else starts the hidden rest.
HEAD = re.compile(r"(?i)([a-z][a-z0-9+.-]{0,31}):[\\/]{1,2}")
HOST = re.compile(r"(?:[A-Za-z0-9._~-]*|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?")
SEGMENT = re.compile(r"[A-Za-z0-9._~!$'()*+,:-]*")
NAME = re.compile(r"[A-Za-z0-9._~\[\]-]*")
VALUE = re.compile(r"[A-Za-z0-9._~+,-]*")
HOST_END = re.compile(r"[/?#\\]")
QUERY = re.compile(r"[?#]")
PARTS = re.compile(r"([?&;#])")
# A longer text holding an address is hidden whole rather than checked.
MAX_TEXT = 256 * 1024
TOO_LONG = f"{HIDDEN} (hidden: too long to check for credentials)"
# The k3s and RKE2 Helm controller: its Job's pods carry the chart's label,
# and the Jobs are named for what they do. Rancher runs helm in
# cattle-system's helm-operation-* pods.
CHART_LABEL = "helmcharts.helm.cattle.io/chart"
HELM_JOBS = ("helm-install-", "helm-delete-")
RANCHER = ("cattle-system", "helm-operation-")


def reveal_role():
    import homestead_routes as ROUTER
    return ROUTER.role(REVEAL[1], REVEAL[0]) or "admin"


def sees_values(role):
    import homestead_auth as AUTH
    return AUTH.allows(role, reveal_role())


def _query(text):
    """The query and fragment ("?..." or "#..."), each parameter of a
    credential's name (or with an odd value) as name=•••; from a name that
    is not plain, the rest is hidden."""
    out = []
    for index, piece in enumerate(PARTS.split(text)):
        if index % 2 or not piece:
            out.append(piece)
            continue
        name, equals, value = piece.partition("=")
        if not NAME.fullmatch(name) or (not equals and SECRET_PARAM.search(name)):
            return "".join(out) + HIDDEN
        out.append(f"{name}={HIDDEN}" if equals and (SECRET_PARAM.search(name) or not VALUE.fullmatch(value)) else piece)
    return "".join(out)


def _mask_token(token):
    """token without its credentials (see the module's notes); a token with
    none is returned as it is."""
    head = HEAD.match(token)
    shown = head.group(1) + "://"
    body = token[head.end():]
    query = QUERY.search(body)
    at = body.rfind("@", 0, query.start() if query else len(body))
    login = HIDDEN + "@" if at >= 0 else ""
    body = body[at + 1:]
    end = HOST_END.search(body)
    host, rest = (body[:end.start()], body[end.start():]) if end else (body, "")
    if not HOST.fullmatch(host) or rest[:1] == "\\":       # some parsers read on past a backslash
        return shown + HIDDEN
    out = shown + login + host
    query = QUERY.search(rest)
    path, rest = (rest[:query.start()], rest[query.start():]) if query else (rest, "")
    for index, segment in enumerate(path.split("/")):
        if not SEGMENT.fullmatch(segment) or SECRET_WORD.search(segment):
            return out + ("/" if index else "") + HIDDEN
        out += ("/" if index else "") + segment
    out += _query(rest)
    return out if login or HIDDEN in out else token


def _strip_text(text):
    return TOKEN.sub(lambda found: _mask_token(found.group(0)), text)


def _unique(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key twice")
    return dict(pairs)


def _strip_json(text):
    """A JSON object or list with each string stripped and written back
    compact, or text itself when nothing changed; None when it is not one
    (or a key would change, or a key is there twice: then it is stripped as
    text)."""
    try:
        data = json.loads(text, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, (dict, list)):
        return None
    changed, todo = False, [data]
    while todo:
        node = todo.pop()
        pairs = node.items() if isinstance(node, dict) else enumerate(node)
        for key, item in list(pairs):
            if isinstance(key, str) and _strip_text(key) != key:
                return None
            if isinstance(item, str):
                stripped = _strip_text(item)
                if stripped != item:
                    node[key], changed = stripped, True
            elif isinstance(item, (dict, list)):
                todo.append(item)
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False) if changed else text


def strip_url_secrets(text):
    """text with the credentials of every URL token masked (see the
    module's notes); a text over MAX_TEXT holding one is hidden whole.
    Stripping twice changes nothing."""
    if not isinstance(text, str) or (":/" not in text and ":\\" not in text):
        return text
    if len(text) > MAX_TEXT:
        return TOO_LONG
    if text.lstrip(SPACE)[:1] in ("{", "["):
        stripped = _strip_json(text)
        if stripped is not None:
            return stripped
    return _strip_text(text)


def cut(text, length, mark="…"):
    """text shortened to length characters, never inside a URL token: one
    that would be cut goes whole. Tokens end only at ASCII whitespace, so a
    cut text holds whole tokens of the full text, and masking it gives the
    same as masking first and cutting after."""
    text = str(text or "")
    if len(text) <= length:
        return text
    for found in TOKEN.finditer(text):
        if found.start() >= length:
            break
        if found.end() > length:
            return text[:found.start()] + mark
    return text[:length]


def squeeze(text):
    """Runs of ASCII whitespace as one space. Only ASCII whitespace ends a
    URL token, so this never joins or splits one: masking before or after
    gives the same."""
    return SPACES.sub(" ", str(text or "")).strip(SPACE)


def strip_in_place(node, at=""):
    """Strips every text in a map or list, in place; returns the paths changed."""
    changed = []
    pairs = list(node.items()) if isinstance(node, dict) else list(enumerate(node)) if isinstance(node, list) else []
    for key, item in pairs:
        path = f"{at}[{key}]" if isinstance(node, list) else f"{at}.{key}" if at else str(key)
        if isinstance(item, str):
            stripped = strip_url_secrets(item)
            if stripped != item:
                node[key] = stripped
                changed.append(path)
        else:
            changed += strip_in_place(item, path)
    return changed


def has_stripped(value):
    """Whether a value holds what strip_url_secrets puts in (an address
    holding "•••", or the too-long note): saved back, it would replace the
    real credential with the mask."""
    if isinstance(value, dict):
        return any(has_stripped(item) for item in value.values())
    if isinstance(value, list):
        return any(has_stripped(item) for item in value)
    return isinstance(value, str) and (TOO_LONG in value or (HIDDEN in value and any(
        HIDDEN in found.group(0) for found in TOKEN.finditer(value))))


def restore(given, stored, at=""):
    """Puts back, in place, every text of given that holds a mask and is
    exactly what the text at the same place in stored shows below reveal;
    returns the paths put back. Any other masked text raises ValueError:
    saved, the mask would replace the credential."""
    put = []
    pairs = list(given.items()) if isinstance(given, dict) else list(enumerate(given)) if isinstance(given, list) else []
    for key, item in pairs:
        path = f"{at}[{key}]" if isinstance(given, list) else f"{at}.{key}" if at else str(key)
        if isinstance(stored, dict):
            other = stored.get(key) if isinstance(given, dict) else None
        else:
            other = stored[key] if isinstance(stored, list) and isinstance(given, list) and key < len(stored) else None
        if isinstance(item, str):
            if not has_stripped(item):
                continue
            if not isinstance(other, str) or strip_url_secrets(other) != item:
                raise ValueError(f"{path} is hidden in this copy and is not what is stored there: type the whole "
                                 "address in again, or open the object as an admin (and reveal a Secret) and edit that copy")
            given[key] = other
            put.append(path)
        else:
            put += restore(item, other, path)
    return put


def helm_job_pod(pod):
    """Whether a pod is one the Helm controller (or Rancher) runs `helm` in."""
    meta = (pod or {}).get("metadata") or {}
    return (CHART_LABEL in (meta.get("labels") or {})
            or (meta.get("namespace") == RANCHER[0] and str(meta.get("name", "")).startswith(RANCHER[1]))
            or any(str(o.get("apiVersion", "")).startswith("helm.cattle.io/")
                   or (o.get("kind") == "Job" and str(o.get("name", "")).startswith(HELM_JOBS))
                   for o in meta.get("ownerReferences") or []))


def for_role(role, answer):
    """answer as a role sees it: unchanged for the role that may reveal a
    Secret; for any other, a copy with every address stripped (the original
    may be a cached answer shared with admins)."""
    if sees_values(role):
        return answer
    answer = copy.deepcopy(answer)
    if isinstance(answer, str):
        return strip_url_secrets(answer)
    strip_in_place(answer)
    return answer


def host(url):
    """Where an address points, as host[:port], never its login: what a page
    shows as an image's source. Empty when it does not parse."""
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        port = parts.port
    except ValueError:
        return ""
    if not parts.hostname:
        return ""
    name = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{name}:{port}" if port else name
