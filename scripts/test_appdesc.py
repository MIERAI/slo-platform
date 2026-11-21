"""scripts/appdesc.py: where an instance keeps its applications and experiments (platform.yaml instance.apps,
regression.experiments) and the metadata of an application's namespaces (as the ApplicationSet sets it)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appdesc  # noqa: E402


class Layout(unittest.TestCase):
    def test_apps_dir(self):
        self.assertEqual(appdesc.apps_dir({}), "apps")
        self.assertEqual(appdesc.apps_dir(None), "apps")
        self.assertEqual(appdesc.apps_dir({"instance": {"apps": "."}}), ".")
        self.assertEqual(appdesc.apps_dir({"instance": {"apps": "deploy/apps"}}), "deploy/apps")

    def test_experiment_dirs(self):
        self.assertEqual(appdesc.experiment_dirs({}), ["apps/*/experiments"])
        self.assertEqual(appdesc.experiment_dirs({"instance": {"apps": "."}}), ["*/experiments"])
        self.assertEqual(appdesc.experiment_dirs({"regression": {"experiments": "experiments"}}), ["experiments"])
        self.assertEqual(appdesc.experiment_dirs({"regression": {"experiments": ["a", "b/*"]}}), ["a", "b/*"])


class NamespaceMetadata(unittest.TestCase):
    def test_default(self):
        self.assertEqual(appdesc.namespace_metadata({"name": "shop"}, "dev"), {
            "labels": {"slo-platform/app": "shop", "slo-platform/env": "dev"},
            "annotations": {"chaos-mesh.org/inject": "enabled"}})
        self.assertEqual(appdesc.namespace_metadata({"name": "shop", "signedImages": True}, "dev")["labels"],
                         {"slo-platform/app": "shop", "slo-platform/env": "dev", "slo-platform/image-signatures": "required"})

    def test_precedence(self):
        """platform-wide, app.yaml over it, the platform's own over both"""
        platform = {"applications": {"namespaceLabels": {"a": "platform", "b": "platform", "slo-platform/app": "x"},
                                     "namespaceAnnotations": {"n": "platform", "chaos-mesh.org/inject": "no"}}}
        d = {"name": "shop", "namespace": {"labels": {"b": "app", "slo-platform/env": "y"}, "annotations": {"n": "app"}}}
        self.assertEqual(appdesc.namespace_metadata(d, "prod", platform), {
            "labels": {"a": "platform", "b": "app", "slo-platform/app": "shop", "slo-platform/env": "prod"},
            "annotations": {"n": "app", "chaos-mesh.org/inject": "enabled"}})


if __name__ == "__main__":
    unittest.main()
