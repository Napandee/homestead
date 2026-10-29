"""What a role below the one that may reveal a Secret is shown of values that
can carry a credential outside a Secret.

- An address's user and password (https://user:pass@host) and the values of
  query parameters named like a credential (?token=..., &sig=...) become
  "•••", wherever the address sits in a text: a chart's repository, a
  webhook's URL, an environment value, an argument.
- The Helm controller's install and delete pods run `helm` with the chart's
  values, and a chart may print them, so their logs go only to that role.

The role is the one GET /api/resources/reveal declares, read from the route
table so the two cannot drift apart."""
import re
import urllib.parse

HIDDEN = "•••"
REVEAL = ("GET", "/api/resources/reveal")
# The text is written by anyone who can set an annotation or cause an event,
# so every pattern here is bounded or scans forward once: the whole strip is
# linear in the text's length.
# A scheme ("https://"): at most 32 characters, so each position is tried in
# bounded time.
SCHEME = re.compile(r"(?i)[a-z][a-z0-9+.-]{0,31}://")
# Where an address ends: whitespace, a quote or an angle bracket. One closing
# mark right at its end (")", "]", "}" or ",") belongs to the text around it.
ADDRESS_END = re.compile(r"[\s\"'<>]")
CLOSERS = ")]},"
# Where the user and password end: the host's path, query or fragment.
HOST_END = re.compile(r"[/?#\s]")
# A query or fragment parameter's name, or Azure's ";Name=": a value runs
# from it to the next separator or the end of the address.
PARAM = re.compile(r"([?&;#])([^=&#;?/\s\"'<>]{1,200})=")
VALUE_END = re.compile(r"[&#;\s\"'<>]")
# Names a credential goes by: tokens, keys, passwords, signatures (S3's
# X-Amz-Signature and X-Amz-Credential, Azure's sig and SharedAccessKey),
# sessions. Hiding a harmless one too is the safe side.
SECRET_PARAM = re.compile(r"pass|^pw$|pwd|secret|token|sig|credential|auth|session|jwt|bearer|cookie|private|"
                          r"key$|^code$", re.I)
ENCODED_SCHEME = re.compile(r"(?i)%3a%2f%2f")
STRIPPED = re.compile(r"://" + HIDDEN + "@|[?&;#][^=&#;?/\\s\"'<>]{1,200}=" + HIDDEN + r"(?=$|[&#;\s\"'<>)\]},])")
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


def _end(pattern, text, start):
    found = pattern.search(text, start)
    return found.start() if found else len(text)


def _userinfo(address):
    """Each user and password in an address, anchored at each scheme in it
    (one can sit inside another's query), up to the last "@" before the
    host's path, as git and curl read it."""
    out, pos = [], 0
    for scheme in SCHEME.finditer(address):
        start = scheme.end()
        if start < pos:
            continue
        at = address.rfind("@", start, _end(HOST_END, address, start))
        if at >= 0:
            out += [address[pos:start], HIDDEN]
            pos = at
    return "".join(out) + address[pos:]


def _params(address, decode):
    """Credential-like parameter values, each masked to its separator or the
    end of the address, whatever it holds. A value that is an address passed
    on encoded (?next=https%3A%2F%2Fuser%3Apass%40...) and carries a
    credential is masked whole (decoded once, never again inside it)."""
    out, pos, start = [], 0, 0
    # One stretch between separators at a time: a value (and any parameter
    # written inside it, ?next=https://host/?token=...) ends at its end.
    while start < len(address):
        end = _end(VALUE_END, address, start + 1)
        first = None
        for param in PARAM.finditer(address, start, end):
            first = first or param
            if param.end() < end and SECRET_PARAM.search(param.group(2)):
                out += [address[pos:param.end()], HIDDEN]
                pos = end
                break
        else:
            value = address[first.end():end] if first else ""
            if decode and value and ENCODED_SCHEME.search(value) and _secret_inside(value):
                out += [address[pos:first.end()], HIDDEN]
                pos = end
        start = end
    return "".join(out) + address[pos:]


def _secret_inside(value):
    plain = urllib.parse.unquote(value)
    return _strip(plain, decode=False) != plain


def _strip(text, decode):
    out, pos = [], 0
    for scheme in SCHEME.finditer(text):
        if scheme.start() < pos:
            continue            # inside an address already done
        end = _end(ADDRESS_END, text, scheme.end())
        if end > scheme.end() and text[end - 1] in CLOSERS:
            end -= 1
        address = text[scheme.start():end]
        out += [text[pos:scheme.start()], _params(_userinfo(address), decode)]
        pos = end
    return "".join(out) + text[pos:]


def strip_url_secrets(text):
    """text with each address's user, password and credential-like query
    values replaced by HIDDEN. Stripping twice changes nothing; the time it
    takes is linear in the text's length."""
    return _strip(text, decode=True) if isinstance(text, str) and "://" in text else text


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
    """Whether a value holds an address stripped here: saved back, it would
    replace the real credential with the mask."""
    if isinstance(value, dict):
        return any(has_stripped(item) for item in value.values())
    if isinstance(value, list):
        return any(has_stripped(item) for item in value)
    return isinstance(value, str) and bool(STRIPPED.search(value))


def helm_job_pod(pod):
    """Whether a pod is one the Helm controller (or Rancher) runs `helm` in."""
    meta = (pod or {}).get("metadata") or {}
    return (CHART_LABEL in (meta.get("labels") or {})
            or (meta.get("namespace") == RANCHER[0] and str(meta.get("name", "")).startswith(RANCHER[1]))
            or any(str(o.get("apiVersion", "")).startswith("helm.cattle.io/")
                   or (o.get("kind") == "Job" and str(o.get("name", "")).startswith(HELM_JOBS))
                   for o in meta.get("ownerReferences") or []))
