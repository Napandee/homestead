"""What a role below the one that may reveal a Secret is shown of values that
can carry a credential outside a Secret.

- An address's user and password (https://user:pass@host) and the values of
  parameters named like a credential (?token=..., #access_token=...,
  ;SharedAccessKey=...) become "•••", wherever the address sits in a text: a
  chart's repository, a webhook's URL, an environment value, an argument.
  Each address is parsed (urllib.parse.urlsplit), and anything that does not
  parse cleanly, or a text too long to check, is hidden whole: when in doubt
  this hides more, never less.
- The Helm controller's install and delete pods run `helm` with the chart's
  values, and a chart may print them, so their logs go only to that role.

The role is the one GET /api/resources/reveal declares, read from the route
table so the two cannot drift apart."""
import copy
import re
import urllib.parse

HIDDEN = "•••"
REVEAL = ("GET", "/api/resources/reveal")
# The text is written by anyone who can set an annotation or cause an event,
# so finding addresses is one bounded regex and the rest is linear parsing.
# An address: a scheme of at most 32 characters, "://", then everything up
# to whitespace, a quote or an angle bracket.
ADDRESS = re.compile(r"(?i)[a-z][a-z0-9+.-]{0,31}://[^\s\"'<>]*")
# One closing mark right at an address's end belongs to the text around it.
CLOSERS = ")]},"
# A longer text holding an address is hidden whole rather than checked.
MAX_TEXT = 256 * 1024
TOO_LONG = f"{HIDDEN} (hidden: too long to check for credentials)"
# An address inside an address's path (not a parameter) is checked once more;
# one nested deeper still is hidden whole.
MAX_DEPTH = 1
# Names a credential goes by: tokens, keys, passwords, signatures (S3's
# X-Amz-Signature and X-Amz-Credential, Azure's sig and SharedAccessKey),
# sessions. Matched on the decoded name, whatever characters it holds.
# Hiding a harmless one too is the safe side.
SECRET_PARAM = re.compile(r"pass|^pw$|pwd|secret|token|sig|credential|auth|session|jwt|bearer|cookie|private|"
                          r"key$|^code$", re.I)
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


def _value_hidden(name, value):
    """Whether a parameter's value is hidden: its decoded name says it is a
    credential, or the value is (or, decoded once, holds) another address or
    a credential's name."""
    if SECRET_PARAM.search(urllib.parse.unquote_plus(name)):
        return True
    if "://" in value:
        return True
    if "%" in value or "+" in value:
        plain = urllib.parse.unquote_plus(value)
        # Decoded once; an address still encoded inside is hidden as well.
        return "://" in plain or "%3a%2f%2f" in plain.lower() or bool(SECRET_PARAM.search(plain))
    return False


def _pairs(text, separators, nested=False):
    """name=value pairs split on separators; each hidden value becomes HIDDEN,
    and every other byte is kept as written. With nested (a query or
    fragment), a piece holding another address is hidden whole even without
    "=": what cannot be fully understood is hidden."""
    out = []
    for piece in re.split(f"([{re.escape(separators)}])", text):
        name, eq, value = piece.partition("=")
        if eq and value and value != HIDDEN and _value_hidden(name, value):
            piece = f"{name}={HIDDEN}"
        elif nested and "://" in piece:
            piece = HIDDEN
        out.append(piece)
    return "".join(out)


def _address(url, depth):
    """One address with its user, password and credential values hidden; the
    whole address, if it does not parse cleanly."""
    scheme, _, rest = url.partition("://")
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018  (raises on a malformed port)
    except ValueError:
        return f"{scheme}://{HIDDEN}"
    # The same split urlsplit makes, kept as written (urlunsplit would drop
    # an empty "?" or "#" and so change the text).
    netloc_end = min([i for i in (rest.find("/"), rest.find("?"), rest.find("#")) if i >= 0] or [len(rest)])
    netloc, tail = rest[:netloc_end], rest[netloc_end:]
    tail, hash_, fragment = tail.partition("#")
    path, question, query = tail.partition("?")
    if netloc != parts.netloc:
        return f"{scheme}://{HIDDEN}"
    if "@" in netloc:
        netloc = HIDDEN + "@" + netloc.rpartition("@")[2]
    if "://" in path:
        if depth >= MAX_DEPTH:
            return f"{scheme}://{HIDDEN}"
        path = _strip(path, depth + 1)
    path = _pairs(path, ";")
    if question:
        query = _pairs(query, "&;", nested=True)
    if hash_ and ("=" in fragment or "://" in fragment):
        fragment = _pairs(fragment, "&;", nested=True)
    return f"{scheme}://{netloc}{path}{question}{query}{hash_}{fragment}"


def _strip(text, depth):
    out, pos = [], 0
    for found in ADDRESS.finditer(text):
        start, end = found.span()
        if end - start > len(found.group(0).partition("://")[0]) + 3 and text[end - 1] in CLOSERS:
            end -= 1
        out += [text[pos:start], _address(text[start:end], depth)]
        pos = end
    return "".join(out) + text[pos:]


def strip_url_secrets(text):
    """text with each address's user, password and credential-like parameter
    values replaced by HIDDEN; a text over MAX_TEXT holding an address is
    hidden whole. Stripping twice changes nothing, and the time it takes is
    linear in the text's length."""
    if not isinstance(text, str) or "://" not in text:
        return text
    if len(text) > MAX_TEXT:
        return TOO_LONG
    return _strip(text, 0)


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
    """Whether a value holds what strip_url_secrets puts in (an address's
    "://•••", a parameter's "=•••", a "••• (hidden" note): saved back, it
    would replace the real credential with the mask."""
    if isinstance(value, dict):
        return any(has_stripped(item) for item in value.values())
    if isinstance(value, list):
        return any(has_stripped(item) for item in value)
    if not isinstance(value, str):
        return False
    if HIDDEN + " (hidden" in value:
        return True
    # The mask where strip_url_secrets puts one: after a separator or a
    # name, or before the host it hides the login of.
    at = value.find(HIDDEN)
    while at >= 0:
        if (at and value[at - 1] in "/?#&;=") or value.startswith("@", at + len(HIDDEN)):
            return True
        at = value.find(HIDDEN, at + 1)
    return False


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
