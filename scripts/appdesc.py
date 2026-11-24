"""Application descriptors (<apps>/<app>/app.yaml of an instance repository, <apps> = platform.yaml instance.apps, examples/<app>/app.yaml here; reference:
docs/app-yaml.md): the one place an application is registered. The ApplicationSet (platform/root/files/
applicationset.yaml) derives the Argo CD Applications from them; the scripts derive the same here — which file holds an
image in an environment, and how to render each Application.

Needs PyYAML (CI: the python3-yaml package; locally: `uv run --with pyyaml …` or pip).
"""
import glob
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required (apt install python3-yaml, or pip install pyyaml)")

APPS = "apps"   # where app.yaml files live in an instance by default (platform.yaml instance.apps): keep in step
                # with the root chart (values.yaml, root.appsGlob)
# namespace metadata the platform sets itself: app.yaml / platform.yaml cannot override it
OWN_LABEL_PREFIX = "slo-platform/"
OWN_ANNOTATIONS = {"chaos-mesh.org/inject": "enabled"}


def apps_dir(platform):
    """Where an instance keeps its applications (platform.yaml instance.apps; "." = the repository root)."""
    return str(((platform or {}).get("instance") or {}).get("apps") or APPS)


def experiment_dirs(platform):
    """The experiment directories (globs, relative to the instance) of platform.yaml regression.experiments, default
    <apps>/*/experiments (keep in step with the root chart's root.experiments)."""
    x = ((platform or {}).get("regression") or {}).get("experiments")
    if not x:
        a = apps_dir(platform)
        return ["*/experiments" if a == "." else f"{a}/*/experiments"]
    return [x] if isinstance(x, str) else list(x)


def namespace_metadata(d, env, platform=None, chaos=True):
    """{labels, annotations} Argo CD gives namespace <name>-<env> (the ApplicationSet's managedNamespaceMetadata):
    platform.yaml applications.namespaceLabels / namespaceAnnotations, app.yaml namespace.labels / annotations over
    them, the platform's own (slo-platform/*, chaos-mesh.org/inject when the cluster runs chaos-mesh) over both."""
    a = (platform or {}).get("applications") or {}
    ns = d.get("namespace") or {}
    labels = {**(a.get("namespaceLabels") or {}), **(ns.get("labels") or {}),
              "slo-platform/app": d["name"], "slo-platform/env": env}
    if d.get("signedImages"):
        labels["slo-platform/image-signatures"] = "required"
    annotations = {**(a.get("namespaceAnnotations") or {}), **(ns.get("annotations") or {}),
                   **(OWN_ANNOTATIONS if chaos else {})}
    return {"labels": labels, "annotations": annotations}


def load(app, root="."):
    """The descriptor of directory <app> (relative to root, e.g. examples/shop), or None."""
    f = os.path.join(root, app, "app.yaml")
    if not os.path.exists(f):
        return None
    with open(f) as fh:
        d = yaml.safe_load(fh)
    d["dir"] = app
    return d


def all_apps(root=".", apps=APPS):
    """Every descriptor under <root>/<apps>/*/app.yaml (an instance: apps; this repository: examples)."""
    return [load(os.path.relpath(os.path.dirname(f), root), root)
            for f in sorted(glob.glob(os.path.join(root, apps, "*", "app.yaml")))]


def component_of(d, image):
    """The component that deploys image: by the services list, else the only component (which then also holds the
    images that are not a service of their own, e.g. a migration image next to its API)."""
    if image:  # the service's image, or a fork's (same <name>: ghcr.io/<you>/<name>)
        hits = [s for s in d.get("services", []) if s["image"] == image] or \
               [s for s in d.get("services", []) if s["image"].rsplit("/", 1)[-1] == image.rsplit("/", 1)[-1]]
        if len(hits) == 1:
            return next(c for c in d["components"] if c["name"] == hits[0]["component"])
    if len(d["components"]) == 1:
        return d["components"][0]
    if image:
        sys.exit(f"{d['dir']}/app.yaml: no service with image {image}")
    sys.exit(f"{d['dir']}/app.yaml: several components, name the image")


def component_file(d, c, env):
    """The file of component c that holds the image tags in env."""
    if "helm" in c:
        return f"{d['dir']}/{c['helm']}/values-{env}.yaml"
    return f"{d['dir']}/{c['kustomize']}/{env}/kustomization.yaml"


def traffic_envs(d):
    """Environments that get synthetic traffic: all, except trafficScale 0."""
    return [e for e in d["environments"] if e.get("trafficScale", 1) != 0] if "traffic" in d else []


def applications(d, root=".", product=None, day=None):
    """What the ApplicationSet makes of descriptor d (keep in step with platform/root/files/applicationset.yaml).
    root: the instance (d's components); product: the product checkout (platform/slo, platform/traffic; default root);
    day: the traffic chart's day settings from platform.yaml ({minutes, tzOffsetHours}). Each entry's "root" is the
    checkout its path is relative to."""
    product = root if product is None else product
    out = []
    for c in d["components"]:
        for e in d["environments"]:
            env = e["name"]
            if "helm" in c:
                path, values = f"{d['dir']}/{c['helm']}/chart", [f"../values-{env}.yaml"]
            else:
                path, values = f"{d['dir']}/{c['kustomize']}/{env}", []
            out.append({"name": f"{c['name']}-{env}", "path": path, "namespace": f"{d['name']}-{env}", "values": values,
                        "release": c["name"], "enforced": True, "root": root})
    if "slos" in d:  # the platform's SLO chart, values from app.yaml
        for e in d["environments"]:
            out.append({"name": f"{d['name']}-slo-{e['name']}", "path": "platform/slo", "namespace": f"{d['name']}-{e['name']}",
                        "values": [], "release": f"{d['name']}-slo", "enforced": True, "root": product,
                        "values_object": {"app": d["name"], "env": e["name"], "slos": d["slos"]}})
    for e in traffic_envs(d):  # the platform's synthetic users, values from app.yaml
        v = {"app": d["name"], "env": e["name"], "requests": d["traffic"]["requests"]}
        if "peakRpm" in d["traffic"]:
            v["peakRpm"] = d["traffic"]["peakRpm"]
        if "trafficScale" in e:
            v["scale"] = e["trafficScale"]
        if day is not None:
            v["day"] = dict(day)
        out.append({"name": f"{d['name']}-traffic-{e['name']}", "path": "platform/traffic",
                    "namespace": f"{d['name']}-{e['name']}", "values": [], "release": f"{d['name']}-traffic",
                    "enforced": True, "values_object": v, "root": product})
    return out
