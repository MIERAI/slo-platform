"""Render what Argo CD renders for one cluster of an instance: the root chart (platform/root, values = the instance's
platform.yaml + cluster), then every Application it makes whose source is the product or the instance repository,
including those the ApplicationSet derives from each <apps>/<app>/app.yaml (platform.yaml instance.apps): a kustomize
directory (kubectl kustomize), a directory of plain manifests (e.g. clusters.<name>.extras without a kustomization) or
a Helm chart with value files / a values object (helm template). Applications of external charts (Argo CD fetches
them from their Helm repositories) and the root Application itself are listed by `root` only. Used by validate.sh and
the scripts.

  python3 scripts/render.py [options] root            the root chart's manifests (Applications, ApplicationSet)
  python3 scripts/render.py [options] list [product]  one line per Application: <name> <path> <namespace> <enforced>
  python3 scripts/render.py [options] render <name> [product]   its manifests
  python3 scripts/render.py [options] all <dir> [product]       every Application's manifests into <dir>/<name>.yaml,
                                                                  and the list (as `list`) on stdout
options:
  --instance DIR    the instance repository's checkout (default: a temporary instance assembled from the product's
                    examples/, laid out as --platform says: see example_instance)
  --platform FILE   its platform.yaml (default: <instance>/platform.yaml; without --instance:
                    platform/root/ci/platform.yaml)
  --cluster NAME    which cluster (default: the first one in platform.yaml)
product: the product checkout (default: this one; e.g. the PR's base). Needs helm, kubectl and PyYAML.
"""
import atexit
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # appdesc (python3 -I does not add the script's dir)
import appdesc  # noqa: E402
import yaml  # noqa: E402   (appdesc has checked it is there)

PRODUCT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_PLATFORM = os.path.join("platform", "root", "ci", "platform.yaml")
_examples = {}   # (product, platform.yaml) → assembled instance directory (one per process)


def example_instance(product=PRODUCT, platform=None):
    """A temporary instance repository made of the product's examples, laid out as platform.yaml (default
    platform/root/ci/platform.yaml) says: each examples/<app> at <instance.apps>/<app> (as `init` copies them); when
    regression.experiments is one plain directory (no glob), the experiments move there from the applications'
    experiments/; then the files of the directory next to that platform.yaml with its name (platform/root/ci/<name>/,
    e.g. what clusters.*.extras point to), if there is one, are copied over the result."""
    product = os.path.abspath(product)
    platform = os.path.abspath(platform or os.path.join(product, EXAMPLE_PLATFORM))
    key = (product, platform)
    if key not in _examples:
        with open(platform) as f:
            values = yaml.safe_load(f) or {}
        d = tempfile.mkdtemp(prefix="slo-instance-")
        atexit.register(shutil.rmtree, d, True)
        apps = os.path.normpath(os.path.join(d, appdesc.apps_dir(values)))
        os.makedirs(apps, exist_ok=True)
        xdirs = appdesc.experiment_dirs(values)
        moved = xdirs[0] if len(xdirs) == 1 and not glob.has_magic(xdirs[0]) else None
        for f in sorted(glob.glob(os.path.join(product, "examples", "*", "app.yaml"))):
            app = os.path.dirname(f)
            dst = os.path.join(apps, os.path.basename(app))
            shutil.copytree(app, dst)
            if moved and os.path.isdir(os.path.join(dst, "experiments")):
                os.makedirs(os.path.join(d, moved), exist_ok=True)
                for x in glob.glob(os.path.join(dst, "experiments", "*.yaml")):
                    shutil.move(x, os.path.join(d, moved, os.path.basename(x)))
                shutil.rmtree(os.path.join(dst, "experiments"))
        extra = os.path.splitext(platform)[0]
        if os.path.isdir(extra):
            shutil.copytree(extra, d, dirs_exist_ok=True)
        shutil.copy(platform, os.path.join(d, "platform.yaml"))
        _examples[key] = d
    return _examples[key]


def _run(cmd, what, stdin=""):
    r = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    if r.returncode != 0:
        sys.exit(f"{what}: {' '.join(cmd)}\n{r.stderr}")
    return r.stdout


def context(product=".", instance=None, platform=None, cluster=None):
    """→ {product, instance, platform (file), values, cluster}: what one rendering is for."""
    product = os.path.abspath(product)
    instance = os.path.abspath(instance) if instance else example_instance(product, platform)
    platform = os.path.abspath(platform) if platform else os.path.join(instance, "platform.yaml")
    with open(platform) as f:
        values = yaml.safe_load(f) or {}
    clusters = list((values.get("clusters") or {}).keys())
    if cluster is None and clusters:
        cluster = clusters[0]
    return {"product": product, "instance": instance, "platform": platform, "values": values, "cluster": cluster}


def root_manifests(ctx):
    """helm template platform/root as the root Application does (valueFiles platform.yaml, parameter cluster)."""
    cmd = ["helm", "template", "slo-platform", os.path.join(ctx["product"], "platform", "root"), "--namespace", "argocd",
           "-f", ctx["platform"]]
    if ctx["cluster"] is not None:
        cmd += ["--set", f"cluster={ctx['cluster']}"]
    return _run(cmd, "platform/root")


def _git_generator(o):
    """The git generator anywhere in an ApplicationSet's generators."""
    if isinstance(o, dict):
        if "git" in o:
            return o["git"]
        o = list(o.values())
    if isinstance(o, list):
        for x in o:
            g = _git_generator(x)
            if g:
                return g
    return None


def _day(ctx):
    """The traffic chart's day from platform.yaml (as the root chart puts it into the ApplicationSet)."""
    with open(os.path.join(ctx["product"], "platform", "root", "files", "timezones.yaml")) as f:
        offsets = yaml.safe_load(f)
    v = ctx["values"]
    return {"minutes": (v.get("traffic") or {}).get("dayMinutes", 1440),
            "tzOffsetHours": offsets[v.get("timezone", "Etc/UTC")]}


def apps(root=".", instance=None, platform=None, cluster=None):
    """Every Application of the cluster whose source is the product (root: its checkout) or the instance repository,
    then those the ApplicationSet derives from every <apps>/<app>/app.yaml. Each: name, path, namespace, values (value
    files relative to the path), release, enforced (an application's, not the platform's), root (the checkout path is
    relative to), values_object (if any)."""
    ctx = context(root, instance, platform, cluster)
    v = ctx["values"]
    repos = {v["product"]["repo"]: ctx["product"], v["instance"]["repo"]: ctx["instance"]}
    out, sets = [], []
    for d in yaml.safe_load_all(root_manifests(ctx)):
        if not d:
            continue
        if d["kind"] == "ApplicationSet":
            sets.append(d)
            continue
        src = d["spec"].get("source")
        if d["kind"] != "Application" or not src or src.get("repoURL") not in repos or "path" not in src:
            continue   # the root Application (sources: this chart) or an external chart
        helm = src.get("helm") or {}
        a = {"name": d["metadata"]["name"], "path": src["path"], "namespace": d["spec"]["destination"]["namespace"],
             "values": helm.get("valueFiles") or [], "release": helm.get("releaseName") or d["metadata"]["name"],
             "enforced": False, "root": repos[src["repoURL"]]}
        if "valuesObject" in helm:
            a["values_object"] = helm["valuesObject"]
        out.append(a)
    for s in sets:
        g = _git_generator(s["spec"]["generators"])
        base = repos[g["repoURL"]]
        # as Argo CD's git generator: the files of every entry, minus those of the entries with exclude: true
        # (clusters.<name>.applications.exclude)
        def found(f):
            return set(glob.glob(os.path.join(base, f["path"])))
        excluded = set().union(*[found(f) for f in g["files"] if f.get("exclude")])
        files = set().union(*[found(f) for f in g["files"] if not f.get("exclude")]) - excluded
        for p in sorted(files):
            d = appdesc.load(os.path.relpath(os.path.dirname(p), base), base)
            off = _excluded_environments(ctx, d["name"])
            if off:
                d = {**d, "environments": [e for e in d["environments"] if e["name"] not in off]}
            out += appdesc.applications(d, base, ctx["product"], _day(ctx))
    return out


def _excluded_environments(ctx, app):
    """The environments of app this cluster leaves out, as the ApplicationSet's environments list does
    (root.environmentsIf): clusters.<name>.environments.exclude and clusters.<name>.applications.environments.<app>
    .exclude."""
    c = ((ctx["values"].get("clusters") or {}).get(ctx["cluster"]) or {}) if ctx["cluster"] is not None else {}
    every = (c.get("environments") or {}).get("exclude") or []
    mine = (((c.get("applications") or {}).get("environments") or {}).get(app) or {}).get("exclude") or []
    return set(every) | set(mine)


KUSTOMIZATIONS = ("kustomization.yaml", "kustomization.yml", "Kustomization")


def render(app, root="."):
    """The manifests of one Application (an entry of apps())."""
    d = os.path.join(app.get("root", root), app["path"])
    if not os.path.isdir(d):
        sys.exit(f"{app['name']}: {app['path']} does not exist in {app.get('root', root)}")
    if not os.path.exists(os.path.join(d, "Chart.yaml")) and not any(
            os.path.exists(os.path.join(d, k)) for k in KUSTOMIZATIONS):
        # plain manifests (Argo CD's directory source, not recursive): its YAML / JSON files as they are
        docs = []
        for f in sorted(glob.glob(os.path.join(d, "*"))):
            if os.path.isfile(f) and f.endswith((".yaml", ".yml", ".json")):
                with open(f) as fh:
                    docs.append(fh.read().strip("\n"))
        return "\n---\n".join(docs) + "\n"
    if os.path.exists(os.path.join(d, "Chart.yaml")):
        cmd = ["helm", "template", app["release"], d, "--namespace", app["namespace"]]
        for v in app["values"]:
            cmd += ["-f", os.path.join(d, v)]
        if "values_object" in app:  # helm.valuesObject (JSON is YAML)
            cmd += ["-f", "-"]
    else:
        cmd = ["kubectl", "kustomize", d]
    return _run(cmd, app["name"], json.dumps(app.get("values_object", {})))


def _line(a):
    return f"{a['name']} {a['path']} {a['namespace']} {'enforced' if a['enforced'] else '-'}"


def main(argv):
    opts, args = {}, []
    while argv:
        x = argv.pop(0)
        if x in ("--instance", "--platform", "--cluster") and argv:
            opts[x[2:]] = argv.pop(0)
        else:
            args.append(x)
    usage = "usage: render.py [--instance DIR] [--platform FILE] [--cluster NAME] root | list [product] | " \
            "render <name> [product] | all <dir> [product]"
    if not args:
        sys.exit(usage)
    cmd, rest = args[0], args[1:]
    if cmd == "root" and len(rest) <= 1:
        sys.stdout.write(root_manifests(context(rest[0] if rest else PRODUCT, **opts)))
    elif cmd == "list" and len(rest) <= 1:
        for a in apps(rest[0] if rest else PRODUCT, **opts):
            print(_line(a))
    elif cmd == "render" and 1 <= len(rest) <= 2:
        found = [a for a in apps(rest[1] if len(rest) > 1 else PRODUCT, **opts) if a["name"] == rest[0]]
        if not found:
            sys.exit(f"no Application {rest[0]}")
        sys.stdout.write(render(found[0]))
    elif cmd == "all" and 1 <= len(rest) <= 2:
        os.makedirs(rest[0], exist_ok=True)
        for a in apps(rest[1] if len(rest) > 1 else PRODUCT, **opts):
            with open(os.path.join(rest[0], a["name"] + ".yaml"), "w") as f:
                f.write(render(a))
            print(_line(a))
    else:
        sys.exit(usage)


if __name__ == "__main__":
    main(sys.argv[1:])
