"""Apps that git applies: Flux or Argo CD, or an administrator's mark that
something else does (homestead_updates.managed_by).

A change Homestead makes to such an app lasts until that tool next applies
it, usually within minutes. A review that writes an app's Deployment says so
first (#734), and a rename, which that tool would turn into two copies,
needs its own acknowledgement.
"""
import homestead_updates as UPDATES

# Every review that writes a Deployment's spec. Each answers with "managed",
# so the page and the MCP tools both see who applies the app
# (tests/test_gitops_notice.py keeps this list complete).
REVIEWS = (("POST", "/api/edit/preview"), ("POST", "/api/move/preview"))


def notice(dep):
    """Who applies this app instead of Homestead: {} when nobody does."""
    return UPDATES.managed_by(dep or {})


def require_rename_ack(dep, body):
    """A rename makes the app under its new name and removes the old one;
    Flux or Argo CD then brings the old one back. Refused until the person
    (or the tool acting for them) says they know."""
    managed = notice(dep)
    if managed and body.get("confirm_managed") is not True:
        where = f" ({managed['source']})" if managed.get("source") else ""
        raise ValueError(f"{managed['by']} applies this app{where} and will bring back the old name, so there "
                         "would be two copies. Rename it in git instead, or confirm with confirm_managed: true")
