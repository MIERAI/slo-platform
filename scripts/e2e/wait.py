"""scripts/e2e.sh: wait for the instance to converge (kubectl with the caller's KUBECONFIG).

  wait.py apps <timeout-seconds>         every Application Synced + Healthy, 3 polls in a row. "Every": the root
                                         Application slo-platform, each Application it renders (its status.resources)
                                         and each one the ApplicationSet `applications` generates
  wait.py probe <namespace> <timeout-s>  Prometheus has a successful probe in <namespace> and no ErrorBudgetBurn alert
                                         firing there (a fault experiment needs a clean baseline)

  wait.py unhealthy                      the diagnosis: each Application not Synced + Healthy, why (conditions, the
                                         last sync, its resources that are not Synced / Healthy)

Exit 0 when reached, 1 on timeout (the last state is printed).
"""
import json
import subprocess
import sys
import time
import urllib.parse

POLL = 15
PROM = "/api/v1/namespaces/monitoring/services/kps-prometheus:9090/proxy/api/v1/"


def kubectl_json(*args):
    r = subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f"kubectl {' '.join(args)} failed")
    return json.loads(r.stdout)


def apps_state():
    """(ok, summary): ok when every expected Application exists, is Synced and Healthy."""
    apps = {a["metadata"]["name"]: a for a in kubectl_json("-n", "argocd", "get", "applications")["items"]}
    if "slo-platform" not in apps:
        return False, "root Application slo-platform not there yet"
    expected = {"slo-platform"}
    expected |= {r["name"] for r in apps["slo-platform"].get("status", {}).get("resources", []) if r.get("kind") == "Application"}
    try:
        appset = kubectl_json("-n", "argocd", "get", "applicationset", "applications")
    except RuntimeError:
        return False, f"{len(expected)} Applications expected so far; ApplicationSet `applications` not there yet"
    generated = {r["name"] for r in appset.get("status", {}).get("resources", [])}
    if not generated:
        return False, f"{len(expected)} Applications expected so far; the ApplicationSet has generated none yet"
    expected |= generated
    bad = []
    for name in sorted(expected):
        a = apps.get(name)
        if a is None:
            bad.append(f"{name} (missing)")
            continue
        st = a.get("status", {})
        sync, health = st.get("sync", {}).get("status", "?"), st.get("health", {}).get("status", "?")
        if (sync, health) != ("Synced", "Healthy"):
            bad.append(f"{name} ({sync}/{health})")
    ok = len(expected) - len(bad)
    return not bad, f"{ok}/{len(expected)} Synced+Healthy" + (f"; not yet: {', '.join(bad)}" if bad else "")


def prom(query):
    q = urllib.parse.quote(query)
    r = subprocess.run(["kubectl", "get", "--raw", f"{PROM}query?query={q}"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "Prometheus not reachable")
    return json.loads(r.stdout)["data"]["result"]


def probe_state(ns):
    up = prom(f'max(probe_success{{namespace="{ns}"}})')
    if not up or up[0]["value"][1] != "1":
        return False, f"no successful probe in {ns} yet"
    firing = prom(f'ALERTS{{alertname="ErrorBudgetBurn", alertstate="firing", namespace="{ns}"}}')
    if firing:
        return False, "ErrorBudgetBurn firing in " + ns + ": " + ", ".join(sorted({a["metric"].get("slo", "?") for a in firing}))
    return True, f"{ns}: probe succeeds, no ErrorBudgetBurn firing"


def wait(check, timeout, need=1):
    deadline, last, streak = time.time() + timeout, None, 0
    while True:
        try:
            ok, msg = check()
        except Exception as e:   # the API server or Prometheus not ready yet: keep waiting
            ok, msg = False, f"({e})"
        streak = streak + 1 if ok else 0
        if msg != last:
            print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)
            last = msg
        if streak >= need:
            return 0
        if time.time() > deadline:
            print(f"timed out after {timeout // 60} min: {msg}", flush=True)
            return 1
        time.sleep(POLL)


def unhealthy():
    for a in kubectl_json("-n", "argocd", "get", "applications")["items"]:
        st = a.get("status", {})
        sync, health = st.get("sync", {}).get("status", "?"), st.get("health", {}).get("status", "?")
        if (sync, health) == ("Synced", "Healthy"):
            continue
        print(f"{a['metadata']['name']}: {sync}/{health} {st.get('health', {}).get('message', '')}")
        for c in st.get("conditions", []):
            print(f"    {c.get('type')}: {c.get('message', '')[:400]}")
        op = st.get("operationState") or {}
        if op.get("phase") not in (None, "Succeeded"):
            print(f"    sync {op.get('phase')}: {op.get('message', '')[:400]}")
        for r in st.get("resources", []):
            h = r.get("health") or {}
            if r.get("status") != "Synced" or h.get("status") not in (None, "Healthy"):
                print(f"    {r.get('kind')}/{r.get('name')} ({r.get('namespace', '')}): {r.get('status')}/{h.get('status')} "
                      f"{h.get('message', '')[:200]}")
    return 0


def main(argv):
    if argv == ["unhealthy"]:
        return unhealthy()
    if len(argv) == 2 and argv[0] == "apps":
        return wait(apps_state, int(argv[1]), need=3)
    if len(argv) == 3 and argv[0] == "probe":
        return wait(lambda: probe_state(argv[1]), int(argv[2]), need=2)
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
