"""Unit tests of scripts/experiment-regression.py (no cluster): the lab guard, finding and planning experiments.

  python3 -I scripts/test_experiment_regression.py
"""
import importlib.util
import os
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "regression", os.path.join(os.path.dirname(os.path.abspath(__file__)), "experiment-regression.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def node(name, labels=None):
    return {"metadata": {"name": name, "labels": labels or {}}, "spec": {}}


LAB = node("lab-1", {"slo-platform/lab": "true"})
PROD = node("prod-1")
LABEL = r.LAB_LABEL


class Guard(unittest.TestCase):
    def test_default_label(self):
        self.assertEqual(LABEL, "slo-platform/lab=true")

    def test_lab_needs_the_label_on_every_node(self):
        self.assertTrue(r.is_lab(LABEL, [LAB, node("lab-2", {"slo-platform/lab": "true"})]))
        self.assertFalse(r.is_lab(LABEL, [LAB, PROD]))

    def test_a_cluster_without_the_label_is_never_a_lab(self):
        self.assertFalse(r.is_lab(LABEL, [PROD]))

    def test_label_must_have_the_value(self):
        self.assertFalse(r.is_lab(LABEL, [node("x", {"slo-platform/lab": "false"})]))
        self.assertFalse(r.is_lab(LABEL, [node("x", {"slo-platform/lab": ""})]))

    def test_no_nodes_no_lab(self):
        self.assertFalse(r.is_lab(LABEL, []))

    def test_another_label(self):
        self.assertTrue(r.is_lab("example.com/chaos=yes", [node("x", {"example.com/chaos": "yes"})]))
        self.assertFalse(r.is_lab("example.com/chaos=yes", [LAB]))
        self.assertTrue(r.is_lab("example.com/chaos", [node("x", {"example.com/chaos": "true"})]))   # bare key: true
        self.assertRaises(ValueError, r.parse_label, "=true")


def write(d, name, text):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w") as f:
        f.write(text)


class Plan(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.a = os.path.join(self.root, "examples", "a", "experiments")
        self.b = os.path.join(self.root, "examples", "b", "experiments")
        write(self.a, "a-down.yaml", "namespace: a-dev\nexpect: {fire: [{alert: X}]}\n")
        write(self.a, "a-quiet.yaml", "namespace: a-dev\nexpect: {fire: []}\n")
        write(self.a, "a-old.yaml", "namespace: a-dev\nskip: superseded by a-down\n")
        write(self.a, "a-kind.yaml", "namespace: a-dev\nclusters: [kind]\n")
        write(self.b, "b-down.yaml", "namespace: b-prod\nexpect: {fire: [{alert: X}]}\n")

    def test_glob_of_directories(self):
        files = r.experiment_files([os.path.join(self.root, "examples", "*", "experiments")])
        self.assertEqual([os.path.basename(f) for f in files],
                         ["a-down.yaml", "a-kind.yaml", "a-old.yaml", "a-quiet.yaml", "b-down.yaml"])
        self.assertEqual(len(r.experiment_files([self.b])), 1)

    def test_names_are_unique(self):
        write(self.b, "a-down.yaml", "namespace: b-prod\n")
        with self.assertRaises(ValueError):
            r.experiment_files([self.a, self.b])

    def test_waves_negative_tests_first_and_skips(self):
        queues, skipped = r.plan(r.experiment_files([self.a, self.b]), set(), existing={"a-dev"})
        self.assertEqual([n for n, _ in queues["a-dev"]], ["a-quiet", "a-down"])
        self.assertEqual(sorted(skipped), ["a-kind", "a-old", "b-down"])
        self.assertIn("superseded", skipped["a-old"][1])
        self.assertIn("does not exist", skipped["b-down"][1])
        queues, _ = r.plan(r.experiment_files([self.a]), {"a-down"}, existing={"a-dev"})
        self.assertEqual(list(queues["a-dev"])[0][0], "a-down")
        self.assertEqual(len(queues["a-dev"]), 1)


if __name__ == "__main__":
    unittest.main()
