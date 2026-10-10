"""If a host fails, is there room for what it runs? For every host at once.

"If this host goes down" (homestead_impact) says what moves and where it may
go; it does not say whether the hosts left have the room (#347). This takes
each host in turn as failed and places its apps on the others the way the
scheduler would, by what they reserve:

* Apps and VMs that would stop anyway (pinned, on host-local storage,
  hardware only that host has) or that wait for the host to come back are
  left to the impact preview; DaemonSet pods do not move. Pods the preview
  does not list - a StatefulSet, a platform app - move wherever there is room,
  as the scheduler would move them.
* The rest are placed in tier order - critical, then normal, then low
  (homestead_priority) - largest first, each on the allowed host with the most
  memory left, as the scheduler's LeastAllocated scoring does.
* Critical and normal pods may evict low ones to fit, so a low pod on a
  surviving host can end up waiting; low pods never evict anything. An
  evicted low pod is counted as waiting, though it may find room elsewhere.

Only reservations count. An app that reserves nothing fits anywhere on paper
and uses memory all the same, so the forecast says how many of those move -
the figures are a floor, not a promise. It is a preview, not the scheduler:
it ignores affinity between apps and the order pods actually restart in.
Pods already Pending, with no host, are counted apart: they wait whatever fails.
"""
import homestead_pod_resources as RESOURCES
import homestead_priority as PRIORITY

ORDER = {"critical": 0, "normal": 1, "low": 2}
TIGHT = 0.9              # a host this full after the move is said to be tight
GIB = 2 ** 30


def _takes_work(node):
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in (node.get("status") or {}).get("conditions") or [])
    return ready and not (node.get("spec") or {}).get("unschedulable")


def _live(pod):
    # A terminating pod's room is given back as it goes, and its replacement
    # is a pod of its own.
    return (pod.get("status") or {}).get("phase") not in ("Succeeded", "Failed") \
        and not (pod.get("metadata") or {}).get("deletionTimestamp")


def _daemon(pod):
    return any(o.get("kind") in ("DaemonSet", "Node") for o in (pod.get("metadata") or {}).get("ownerReferences") or [])


def _need(pod):
    spec = pod.get("spec") or {}
    tier = PRIORITY.tier_of(spec)[0]
    # A class Homestead does not keep is placed with the normal tier.
    return {"cpu": RESOURCES.pod_request(spec, "cpu"), "mem": RESOURCES.pod_request(spec, "memory"),
            "tier": tier if tier in ORDER else "normal"}


def forecast(failed, nodes, pods, preview, owner):
    """What happens to the room on the other hosts if failed fails.
    preview: homestead_impact.preview for that host; owner(pod) names the
    workload a pod runs for."""
    survivors = {}
    for node in nodes:
        name = (node.get("metadata") or {}).get("name")
        if name == failed or not _takes_work(node):
            continue
        alloc = (node.get("status") or {}).get("allocatable") or {}
        survivors[name] = {"name": name, "cpu_alloc": RESOURCES.quantity(alloc.get("cpu"), "cpu"),
                           "mem_alloc": RESOURCES.quantity(alloc.get("memory")), "cpu": 0, "mem": 0, "low": []}
    # Where each app and VM may go, from the impact preview. Rows that stop,
    # or wait for this host to come back, need no room elsewhere.
    listed = {(r.get("ns"), r.get("name")): r for r in preview.get("apps") or [] if r.get("kind") in ("app", "vm")}
    moving, pending = [], 0
    for pod in pods:
        if not _live(pod):
            continue
        meta, where = pod.get("metadata") or {}, (pod.get("spec") or {}).get("nodeName")
        if not where:
            pending += (pod.get("status") or {}).get("phase") == "Pending"
            continue
        need = _need(pod)
        if where in survivors:
            host = survivors[where]
            if need["tier"] == "low":
                host["low"].append({"id": f"{meta.get('namespace')}/{owner(pod) or meta.get('name')}", **need})
            else:
                host["cpu"] += need["cpu"]
                host["mem"] += need["mem"]
        elif where == failed and not _daemon(pod):
            app = owner(pod) or meta.get("name")
            row = listed.get((meta.get("namespace"), app))
            if row is None:
                moving.append({"id": f"{meta.get('namespace')}/{app}", "to": list(survivors), **need})
            elif row.get("outcome") == "moves":
                moving.append({"id": f"{meta.get('namespace')}/{app}", "to": row.get("to") or [], **need})
    moving.sort(key=lambda p: (ORDER[p["tier"]], -p["mem"], p["id"]))
    before = {h["name"]: h["mem"] + sum(p["mem"] for p in h["low"]) for h in survivors.values()}

    def fits(host, pod):
        return host["cpu"] + pod["cpu"] <= host["cpu_alloc"] and host["mem"] + pod["mem"] <= host["mem_alloc"]

    waits, evicted = [], []
    # Critical and normal first, into the room left once low pods give way.
    for pod in (p for p in moving if p["tier"] != "low"):
        allowed = [survivors[h] for h in pod["to"] if h in survivors]
        room = sorted((h for h in allowed if fits(h, pod)), key=lambda h: h["mem_alloc"] - h["mem"], reverse=True)
        if room:
            room[0]["cpu"] += pod["cpu"]
            room[0]["mem"] += pod["mem"]
        else:
            waits.append({**pod, "why": "no host it may run on has room, even with low-tier apps evicted"
                          if allowed else "no host it may run on is taking work"})
    # Low pods already there keep their place while room allows, smallest first.
    for host in survivors.values():
        for pod in sorted(host["low"], key=lambda p: p["mem"]):
            if fits(host, pod):
                host["cpu"] += pod["cpu"]
                host["mem"] += pod["mem"]
            else:
                evicted.append({**pod, "on": host["name"], "why": f"evicted from {host['name']} to make room"})
    for pod in (p for p in moving if p["tier"] == "low"):
        room = sorted((survivors[h] for h in pod["to"] if h in survivors and fits(survivors[h], pod)),
                      key=lambda h: h["mem_alloc"] - h["mem"], reverse=True)
        if room:
            room[0]["cpu"] += pod["cpu"]
            room[0]["mem"] += pod["mem"]
        else:
            waits.append({**pod, "why": "low tier: it waits for room and evicts nothing"})
    hosts = [{"name": h["name"], "mem_gib": round(h["mem"] / GIB, 1), "mem_alloc_gib": round(h["mem_alloc"] / GIB, 1),
              "mem_before_gib": round(before[h["name"]] / GIB, 1),
              "cpu_m": h["cpu"], "cpu_alloc_m": h["cpu_alloc"],
              "full": round(max(h["mem"] / h["mem_alloc"] if h["mem_alloc"] else 1, h["cpu"] / h["cpu_alloc"] if h["cpu_alloc"] else 1), 3)}
             for h in sorted(survivors.values(), key=lambda h: h["name"])]
    stuck = waits + evicted
    unreserved = sum(1 for p in moving if not p["mem"] and not p["cpu"])
    verdict = ("no-host" if not survivors and moving else "waits" if stuck
               else "tight" if any(h["full"] >= TIGHT for h in hosts) else "fits")
    return {"node": failed, "verdict": verdict, "moving": len(moving), "hosts": hosts, "pending": pending,
            "waits": [{k: p[k] for k in ("id", "tier", "why")} | {"mem_gib": round(p["mem"] / GIB, 2)} for p in waits],
            "evicted": [{k: p[k] for k in ("id", "tier", "why", "on")} | {"mem_gib": round(p["mem"] / GIB, 2)} for p in evicted],
            "unreserved": unreserved, "sentence": sentence(failed, verdict, hosts, stuck, len(moving))}


def _gib(value):
    return f"{value:g}"


def sentence(failed, verdict, hosts, stuck, moving=1):
    """The forecast in one line, the fullest host first."""
    if not moving:
        return f"If {failed} fails, nothing on it needs room on another host."
    if verdict == "no-host":
        return f"If {failed} fails, no other host takes work: what runs on it waits."
    worst = max(hosts, key=lambda h: h["full"])
    by_cpu = worst["cpu_alloc_m"] and worst["cpu_m"] / worst["cpu_alloc_m"] > (worst["mem_gib"] / worst["mem_alloc_gib"] if worst["mem_alloc_gib"] else 1)
    need = (f"{worst['name']} would have {round(100 * worst['cpu_m'] / worst['cpu_alloc_m'])}% of its CPU reserved" if by_cpu
            else f"{worst['name']} would have {_gib(worst['mem_gib'])} of {_gib(worst['mem_alloc_gib'])} GiB of memory reserved")
    if not stuck:
        return f"If {failed} fails, {need}." + (" Tight, but it fits." if verdict == "tight" else " It fits.")
    tiers = {p["tier"] for p in stuck}
    tier = f"{tiers.pop()}-tier " if len(tiers) == 1 else ""
    evicted = any(p.get("on") for p in stuck)
    return (f"If {failed} fails, {need}; {len(stuck)} {tier}app{'' if len(stuck) == 1 else 's'} "
            f"would {'wait for room or be evicted' if evicted else 'wait for room'}.")
