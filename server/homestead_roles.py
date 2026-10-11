"""Host roles Homestead tells apart beyond Kubernetes' own (#364).

A tiebreaker is a small server kept for an etcd vote - the "small third
server" beside two main hosts. It runs no apps, holds no Longhorn copies and
answers no VIPs. It is marked by the taint node-role/tiebreaker=true:
NoSchedule, the name #373's tests already use, and by a label of the same key:
k3s lets a joining node set that label on itself, which it refuses for
node-role.kubernetes.io/*. Either marks it, so a host set up by hand with
only one of them counts too.
"""
KEY = "node-role/tiebreaker"
TAINT = {"key": KEY, "value": "true", "effect": "NoSchedule"}
LABEL = {KEY: "true"}


def is_tiebreaker(node):
    """node: a Node as the API gives it, or Homestead's node summary, which
    keeps labels and taints at its top level."""
    labels = (node.get("metadata") or {}).get("labels") or node.get("labels") or {}
    if labels.get(KEY) == "true":
        return True
    taints = (node.get("spec") or {}).get("taints") or node.get("taints") or []
    return any(t.get("key") == KEY and t.get("effect") in ("NoSchedule", "NoExecute") for t in taints)


def tiebreakers(nodes):
    """The names of the tiebreakers among nodes."""
    return {(n.get("metadata") or {}).get("name") or n.get("name") for n in nodes if is_tiebreaker(n)}
