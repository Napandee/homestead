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
# An address with a scheme, up to whitespace or a quote.
URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]*")
# The user and password: up to the last "@" before the path, as git and curl
# read it, at every "://" (an address can sit inside another's query).
USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/?#\s]*@")
# A query or fragment parameter, or Azure's ";Name=Value"; the value stops
# before punctuation that closes the text around the address.
PARAM = re.compile(r"([?&;#])([^=&#;?]+)=([^&#;)\]},]+)")
# Names a credential goes by: tokens, keys, passwords, signatures (S3's
# X-Amz-Signature and X-Amz-Credential, Azure's sig and SharedAccessKey),
# sessions. Hiding a harmless one too is the safe side.
SECRET_PARAM = re.compile(r"pass|^pw$|pwd|secret|token|sig|credential|auth|session|jwt|bearer|cookie|private|"
                          r"key$|^code$", re.I)
STRIPPED = re.compile(r"://" + HIDDEN + "@|[?&;#][^=&#;\\s]+=" + HIDDEN + r"(?=$|[^A-Za-z0-9%._~-])")
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


def _param(match):
    sep, name, value = match.groups()
    if SECRET_PARAM.search(name):
        return f"{sep}{name}={HIDDEN}"
    if "%" in value:
        # An address passed on encoded (?next=https%3A%2F%2Fuser%3Apass%40...).
        plain = urllib.parse.unquote(value)
        if "://" in plain and strip_url_secrets(plain) != plain:
            return f"{sep}{name}={HIDDEN}"
    # An address passed on as written: its own query too.
    return f"{sep}{name}={strip_url_secrets(value)}"


def _strip_url(match):
    return PARAM.sub(_param, USERINFO.sub(lambda m: m.group(1) + HIDDEN + "@", match.group(0)))


def strip_url_secrets(text):
    """text with each address's user, password and credential-like query
    values replaced by HIDDEN. Stripping twice changes nothing."""
    return URL.sub(_strip_url, text) if isinstance(text, str) and "://" in text else text


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
