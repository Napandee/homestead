"""What each app's containers use, kept long enough to say what to reserve.

The scheduler places pods by what they reserve (their requests), never by what
they use, so an app without requests is placed as if it used nothing - and a
host can fill up while it looks empty to Kubernetes (#347). One reading from
metrics-server is a poor guide: an app that is idle at noon may compile at
night. This keeps, for every container of every app, its CPU and memory every
STEP seconds, and offers a reservation from the last WINDOW_DAYS.

Each day is a histogram per container rather than a list of samples: buckets
growing by RATIO, so a reading is kept to within 10% in a handful of numbers,
and a week's percentile is that of the week's readings, not an average of
daily ones. The day's largest reading is kept exactly. Days older than
KEEP_DAYS are forgotten, and so is a container no longer seen for that long.

A reading is per pod: a request is per pod too, so an app with three copies
gives three readings each time, and none of them is added up.

Nothing here changes an app. The suggestion is shown in Edit, beside the
fields it would fill, and saved only if the person saves it.
"""
import json
import math
import os
import threading
import time

import homestead_shared as SHARED

DATA_DIR = "/data"
# Kubernetes quantities in cores and bytes; the server's own parsers, bound in.
_cpu = _mem = None
FILE = "usage.json"
STEP = 300
KEEP_DAYS = 14
WINDOW_DAYS = 7
MIN_SAMPLES = 288          # a day of readings before anything is suggested
RATIO = 1.1
PERCENTILE = 0.95
# Headroom over the 95th percentile, and the least worth reserving, as the
# issue proposed: CPU is shared and bursts are throttled, so it gets more
# headroom; memory over the reservation is what kubelet evicts first.
CPU_FACTOR, CPU_FLOOR_M, CPU_STEP_M = 1.5, 10, 5
MEM_FACTOR, MEM_FLOOR_MI, MEM_STEP_MI = 1.25, 32, 16
# A critical app gets more: kubelet, short of memory, evicts the pods most
# over their reservation first - before it looks at priority at all.
MEM_FACTOR_CRITICAL = 1.5
_lock = threading.Lock()


def bind(data_dir, cpu_cores, mem_bytes):
    global DATA_DIR, _cpu, _mem
    DATA_DIR, _cpu, _mem = data_dir, cpu_cores, mem_bytes


def _path():
    return os.path.join(DATA_DIR, FILE)


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) and isinstance(data.get("series"), dict) else {"series": {}}
    except (OSError, ValueError):
        return {"series": {}}


def _day(now):
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _bucket(value):
    """A reading's bucket: -1 for under one unit, else the power of RATIO it
    falls under, so the bucket's top is never below the reading."""
    return -1 if value < 1 else int(math.floor(math.log(value) / math.log(RATIO)))


def _top(bucket):
    return 1.0 if bucket < 0 else RATIO ** (bucket + 1)


def key(ns, workload, container):
    return f"{ns}/{workload}/{container}"


def record(readings, now=None):
    """Adds one round of readings: [(ns, workload, container, millicores, mib)]."""
    now = int(now or time.time())
    today, oldest = _day(now), _day(now - KEEP_DAYS * 86400)
    with _lock:
        data = _load()
        series = data["series"]
        for ns, workload, container, cpu_m, mem_mi in readings:
            entry = series.setdefault(key(ns, workload, container), {"days": {}})
            day = entry["days"].setdefault(today, {"n": 0, "cpu": {}, "mem": {}, "cpu_max": 0, "mem_max": 0})
            day["n"] += 1
            for field, value in (("cpu", cpu_m), ("mem", mem_mi)):
                bucket = str(_bucket(value))
                day[field][bucket] = day[field].get(bucket, 0) + 1
                day[f"{field}_max"] = max(day[f"{field}_max"], round(value, 1))
            entry["seen"] = today
        for name in list(series):
            series[name]["days"] = {d: v for d, v in series[name]["days"].items() if d >= oldest}
            if not series[name]["days"] or series[name].get("seen", "") < oldest:
                series.pop(name)
        SHARED.write_json(_path(), {"format": 1, "series": series}, separators=(",", ":"), sort_keys=True)


def _percentile(histograms, share):
    counts = {}
    for histogram in histograms:
        for bucket, count in histogram.items():
            counts[int(bucket)] = counts.get(int(bucket), 0) + count
    total = sum(counts.values())
    if not total:
        return 0.0
    seen = 0
    for bucket in sorted(counts):
        seen += counts[bucket]
        if seen >= share * total:
            return _top(bucket)
    return _top(max(counts))


def _round_up(value, step):
    return int(math.ceil(value / step) * step)


def _millicores(text):
    return _cpu(text) * 1000 if str(text or "").strip() else None


def _mebibytes(text):
    return _mem(text) / 2 ** 20 if str(text or "").strip() else None


def summary(ns, workload, container, requests=None, limits=None, now=None, tier=""):
    """What a container has used over the last WINDOW_DAYS, and what to
    reserve for it - or why there is no suggestion yet."""
    now = int(now or time.time())
    first = _day(now - (WINDOW_DAYS - 1) * 86400)
    with _lock:
        entry = _load()["series"].get(key(ns, workload, container)) or {}
    days = {d: v for d, v in (entry.get("days") or {}).items() if d >= first}
    samples = sum(v.get("n", 0) for v in days.values())
    out = {"days": len(days), "samples": samples, "window_days": WINDOW_DAYS, "since": min(days) if days else ""}
    if not samples:
        out["why"] = "No readings yet: Homestead records use every five minutes."
        return out
    cpu = _percentile([v.get("cpu") or {} for v in days.values()], PERCENTILE)
    mem = _percentile([v.get("mem") or {} for v in days.values()], PERCENTILE)
    out.update({"cpu_p95_m": round(cpu), "cpu_max_m": round(max(v.get("cpu_max", 0) for v in days.values())),
                "mem_p95_mi": round(mem), "mem_max_mi": round(max(v.get("mem_max", 0) for v in days.values()))})
    if samples < MIN_SAMPLES:
        out["why"] = f"A suggestion needs a day of readings; {samples} of {MIN_SAMPLES} so far."
        return out
    cpu_m = max(CPU_FLOOR_M, _round_up(cpu * CPU_FACTOR, CPU_STEP_M))
    mem_mi = max(MEM_FLOOR_MI, _round_up(mem * (MEM_FACTOR_CRITICAL if tier == "critical" else MEM_FACTOR), MEM_STEP_MI))
    # Kubernetes refuses a request above the limit, so a suggestion never
    # passes one; it says so, since the limit itself may be what is wrong.
    capped = []
    cpu_limit, mem_limit = _millicores((limits or {}).get("cpu")), _mebibytes((limits or {}).get("memory"))
    if cpu_limit and cpu_m > cpu_limit:
        cpu_m = int(cpu_limit)
        capped.append("cpu")
    if mem_limit and mem_mi > mem_limit:
        mem_mi = int(mem_limit)
        capped.append("memory")
    current_cpu, current_mem = _millicores((requests or {}).get("cpu")), _mebibytes((requests or {}).get("memory"))
    out["suggest"] = {"cpu": f"{cpu_m}m", "memory": f"{mem_mi}Mi", "capped": capped,
                      "differs": current_cpu != cpu_m or current_mem != mem_mi}
    return out


def readings(pods, metrics, owner, skip_ns=()):
    """This round's readings from pods and their metrics: running pods outside
    skip_ns that owner() names a workload for; anything else is not an app."""
    by_pod = {(m["metadata"]["namespace"], m["metadata"]["name"]): m for m in metrics or []}
    out = []
    for pod in pods or []:
        meta = pod.get("metadata") or {}
        ns = meta.get("namespace", "")
        if ns in skip_ns or (pod.get("status") or {}).get("phase") != "Running":
            continue
        workload = owner(pod)
        metric = by_pod.get((ns, meta.get("name", "")))
        if not workload or not metric:
            continue
        for row in metric.get("containers") or []:
            usage = row.get("usage") or {}
            cpu, mem = _millicores(usage.get("cpu")), _mebibytes(usage.get("memory"))
            if row.get("name") and cpu is not None and mem is not None:
                out.append((ns, workload, row["name"], cpu, mem))
    return out
