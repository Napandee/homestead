"""What a role below the one that may reveal a Secret is shown of values that
can carry a credential outside a Secret.

- An address (a "URL token": a scheme, "://" - or ":/" or ":\\", which
  other parsers accept - and everything up to the next ASCII whitespace)
  that holds "@", "=", "%" or ";", or a word a credential goes by, is
  replaced whole by "scheme://•••". Nothing is parsed and nothing is masked
  in part: a rule this simple cannot be read differently from the way a
  client reads the address, and a viewer sees less, never more. An address
  without any of those (https://charts.example.com/stable) is shown as it is.
- A text longer than MAX_TEXT that holds an address is hidden whole.
- The Helm controller's install and delete pods run `helm` with the chart's
  values, and a chart may print them, so their logs go only to that role.

The role is the one GET /api/resources/reveal declares, read from the route
table so the two cannot drift apart. Everything here is linear in the
length of the text: anyone who can set an annotation or cause an event
writes it."""
import copy
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
# What makes a token hidden: a login ("@"), a parameter ("=", ";"), anything
# encoded ("%"), or a word a credential goes by (tokens, keys, passwords,
# signatures, sessions; a harmless word hidden too is the safe side).
MARKS = "@=%;"
SECRET_WORD = re.compile(r"pass|pwd|secret|token|sig|credential|auth|session|jwt|bearer|cookie|private|hmac|otp|"
                         r"ticket|apikey|api-key|api_key|access-key|access_key", re.I)
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


def _hidden(token):
    return any(mark in token for mark in MARKS) or bool(SECRET_WORD.search(token))


def _masked(token):
    return token.partition(":")[0] + "://" + HIDDEN


def strip_url_secrets(text):
    """text with every URL token that could carry a credential replaced
    whole by "scheme://•••"; a text over MAX_TEXT holding one is hidden
    whole. Stripping twice changes nothing."""
    if not isinstance(text, str) or (":/" not in text and ":\\" not in text):
        return text
    if len(text) > MAX_TEXT:
        return TOO_LONG
    return TOKEN.sub(lambda found: _masked(found.group(0)) if _hidden(found.group(0)) else found.group(0), text)


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
    """Whether a value holds what strip_url_secrets puts in ("scheme://•••"
    or the too-long note): saved back, it would replace the real credential
    with the mask."""
    if isinstance(value, dict):
        return any(has_stripped(item) for item in value.values())
    if isinstance(value, list):
        return any(has_stripped(item) for item in value)
    return isinstance(value, str) and ("://" + HIDDEN in value or TOO_LONG in value)


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
