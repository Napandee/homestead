"""What Homestead knows about platform version pairs, as data.

The upgrade check (homestead_upgrade_check) reads only this table: no chart
downloads and no fetches at check time. Every row names the page it was read
from, so a person can check it and a maintainer can update it in a one-row
change. A version this table does not list is "not checked", never
"compatible" (#620): add its row, with its source, once someone has read it.

tests/test_upgrade_known.py keeps the table well-formed.
"""

# When the rows below were last read against their sources. Whoever adds or
# changes a row updates this; a new k3s, Longhorn or KubeVirt minor shows as
# "not checked" (or outside the matrix) until someone reads its source here.
CHECKED = "2026-10-23"

# KubeVirt's Kubernetes support matrix: for each KubeVirt minor, the
# Kubernetes minors it supports ("supported", a tick in the matrix) and the
# ones it supported that Kubernetes itself has since ended ("eol"). The matrix
# has columns for Kubernetes 1.31 to 1.36 only: a minor outside those says
# nothing about older ones, and a newer one is not yet in it.
KUBEVIRT_KUBERNETES = {
    "source": "https://github.com/kubevirt/sig-release/blob/f3d8a41eb14bc3f7ee8b29e8fcf5b940523af7a4/releases/k8s-support-matrix.md",
    "columns": ("1.31", "1.36"),
    "rows": {
        "1.9": {"supported": ["1.34", "1.35", "1.36"], "eol": []},
        "1.8": {"supported": ["1.34", "1.35"], "eol": ["1.33"]},
        "1.7": {"supported": ["1.34"], "eol": ["1.32", "1.33"]},
        "1.6": {"supported": [], "eol": ["1.31", "1.32", "1.33"]},
        "1.5": {"supported": [], "eol": ["1.31", "1.32"]},
        "1.4": {"supported": [], "eol": ["1.31"]},
    },
}

# Longhorn's Helm chart, one row per released chart version read:
# kubernetes_min - the chart's kubeVersion floor (Chart.yaml);
# restrict_internal_traffic - whether values.yaml turns
#   networkPolicies.restrictInternalTraffic on by default. When on, Longhorn's
#   NetworkPolicies admit only its own pods to longhorn-backend:9500 (#793);
#   the chart's templates gate them on this key alone.
# Longhorn 1.12.2 is not listed: it was not released when this was checked
# (its rc1 turns the setting on).
# The chart folder at that tag: kubeVersion in Chart.yaml, defaults in values.yaml.
_LONGHORN = "https://github.com/longhorn/longhorn/tree/{}/chart"
LONGHORN_CHARTS = {
    "v1.12.0": {"kubernetes_min": "1.25", "restrict_internal_traffic": False, "source": _LONGHORN.format("v1.12.0")},
    "v1.12.1": {"kubernetes_min": "1.25", "restrict_internal_traffic": True, "source": _LONGHORN.format("v1.12.1")},
    "v1.13.0": {"kubernetes_min": "1.34", "restrict_internal_traffic": True, "source": _LONGHORN.format("v1.13.0")},
}
