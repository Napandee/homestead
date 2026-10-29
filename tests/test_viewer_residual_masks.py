"""What a role below reveal still saw after the resource browser's masks, now hidden.

Builds on tests/test_viewer_helm_values.py (the same objects on the shared fake):
- annotations are shown in full only under named prefixes, not any subdomain
  of a known domain, and never when the key itself says it carries a command,
  headers or a credential;
- a mask says no exact length: a text shows "(hidden)", several lines a
  rounded count, and a masked map keeps only its own keys;
- a URL loses its user and password, and the values of secret-looking query
  parameters, wherever it appears (object, list columns, Helm's source);
- the Helm controller's install and delete pods print the chart's values, so
  their logs go only to the role that may reveal a Secret.
An admin and the reveal route see everything as before."""
import base64
import copy
import gzip
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_agent_tools as TOOLS
import homestead_helm as HELM
import homestead_joblogs as JOBLOGS
import homestead_operations as OPS
import homestead_overview_server as OVERVIEW
import homestead_resources as RES
import homestead_routes as ROUTER
import homestead_value_masks as MASKS
import homestead_workloads as WL
import server  # noqa: F401  (binds the agent tools and the route table)
import test_viewer_helm_values as BASE
from fakes.kube import Kube

LONG_VALUES = "".join(f"key{i}: value{i}\n" for i in range(47))
CHART = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/app"
DEPLOYMENT = "/apis/apps/v1/namespaces/lab/deployments/web"
RELEASE = "/apis/helm.toolkit.fluxcd.io/v2/namespaces/flux/helmreleases/db"
WEBHOOK = "/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations/hook"

EXTRA = {
    # A second chart: credentials in its repository and chart addresses, the
    # copy of the previous values Homestead keeps during an upgrade, and
    # values long enough to round.
    "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/private": {
        "apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
        "metadata": {"name": "private", "namespace": "kube-system", "resourceVersion": "5",
                     "annotations": {"homestead.io/helm-previous": json.dumps({"valuesContent": "password: hunter2\n"}),
                                     "homestead.io/group": "web", "objectset.rio.cattle.io/hash": "abc123",
                                     "helmcharts.cattle.io/managed-by": "helm-controller"}},
        "spec": {"repo": "https://deploy:hunter2@charts.example.com/stable",
                 "chart": "https://charts.example.com/app-1.0.tgz?token=s3cr3t-t0ken&arch=amd64",
                 "valuesContent": LONG_VALUES}},
    # Annotations under well-known domains that still carry secrets.
    "/apis/apps/v1/namespaces/lab/deployments/api": {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "lab", "resourceVersion": "5", "annotations": {
            "kubernetes.io/change-cause": "kubectl set env deployment/api TOKEN=s3cr3t-t0ken",
            "nginx.ingress.kubernetes.io/configuration-snippet": 'more_set_headers "X-Auth: hunter2";',
            "secrets.cattle.io/backup": "hunter2",
            "cdi.kubevirt.io/storage.import.extraHeaders": "Authorization: Bearer s3cr3t-t0ken",
            "deployment.kubernetes.io/revision": "7", "meta.helm.sh/release-name": "api",
            "field.cattle.io/projectId": "local:p-1", "kubevirt.io/latest-observed-api-version": "v1"}},
        "spec": {"replicas": 1, "template": {
            "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "2026-10-28T00:00:00Z",
                                         "nginx.ingress.kubernetes.io/server-snippet": "auth hunter2"}},
            "spec": {"containers": [{
                "name": "api", "image": "example/api:1",
                "args": ["--notify=https://hooks.example.com/post?sig=s3cr3t-t0ken&room=ops"],
                "env": [{"name": "DATABASE_URL", "value": "postgres://app:hunter2@db.example.com:5432/app"},
                        {"name": "HOME_PAGE", "value": "https://example.com/start?page=2"}]}]}}}},
    WEBHOOK: {
        "apiVersion": "admissionregistration.k8s.io/v1", "kind": "MutatingWebhookConfiguration",
        "metadata": {"name": "hook", "resourceVersion": "5"},
        "webhooks": [{"name": "hook.example.com", "clientConfig": {
            "url": "https://hook.example.com/mutate?api_key=s3cr3t-t0ken&X-Amz-Signature=hunter2"}}]},
    # Nothing on it is masked but a URL's credentials.
    "/apis/apps/v1/namespaces/lab/deployments/dburl": {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "dburl", "namespace": "lab", "resourceVersion": "5",
                     "annotations": {"cdi.kubevirt.io/storage.import.endpoint": "https://images.example.com/d.img?token=s3cr3t-t0ken"}},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [{
            "name": "db", "image": "example/db:1",
            "env": [{"name": "DATABASE_URL", "value": "postgres://app:hunter2@db.example.com:5432/app"}]}]}}}},
    "/apis/helm.toolkit.fluxcd.io/v2/namespaces/flux/helmreleases/users": {
        "apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease",
        "metadata": {"name": "users", "namespace": "flux", "resourceVersion": "5"},
        "spec": {"values": {"users": {"alice-the-admin": {"password": "hunter2"}}, "replicas": 1}}},
}
PRIVATE = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/private"
API = "/apis/apps/v1/namespaces/lab/deployments/api"
USERS = "/apis/helm.toolkit.fluxcd.io/v2/namespaces/flux/helmreleases/users"
DBURL = "/apis/apps/v1/namespaces/lab/deployments/dburl"
SECRETS = ("hunter2", "s3cr3t-t0ken", "alice-the-admin")


def _path_parts(path):
    parts = path.strip("/").split("/")
    group, version = ("", parts[1]) if parts[0] == "api" else (parts[1], parts[2])
    namespaced = "namespaces" in parts
    return group, version, parts[-2], parts[-3] if namespaced else "", parts[-1]


class Base(BASE.Base):
    """The base file's objects plus EXTRA, on the shared fake."""

    def setUp(self):
        patcher = mock.patch.dict(BASE.OBJECTS, EXTRA)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def get(self, path, role, what="/api/resources/object"):
        group, version, resource, ns, name = _path_parts(path)
        query = {"group": [group], "version": [version], "resource": [resource], "ns": [ns], "name": [name]}
        return OVERVIEW.ROUTES[("GET", what)][1](ROUTER.Request("GET", what, query, {}, "someone", role))

    def assertHidden(self, answer):
        text = repr(answer)
        for secret in SECRETS:
            self.assertNotIn(secret, text)


class Annotations(Base):
    def test_a_viewer_gets_secret_carrying_annotations_under_known_domains_masked(self):
        for role in ("viewer", "operator", None):
            with self.subTest(role=role):
                answer = self.get(API, role)
                self.assertHidden(answer)
                notes = answer["object"]["metadata"]["annotations"]
                for key in ("kubernetes.io/change-cause", "nginx.ingress.kubernetes.io/configuration-snippet",
                            "secrets.cattle.io/backup", "cdi.kubevirt.io/storage.import.extraHeaders"):
                    self.assertEqual("••• (hidden)", notes[key], key)
                template = answer["object"]["spec"]["template"]["metadata"]["annotations"]
                self.assertEqual("••• (hidden)", template["nginx.ingress.kubernetes.io/server-snippet"])

    def test_plain_annotations_under_named_prefixes_stay(self):
        notes = self.get(API, "viewer")["object"]["metadata"]["annotations"]
        self.assertEqual("7", notes["deployment.kubernetes.io/revision"])
        self.assertEqual("api", notes["meta.helm.sh/release-name"])
        self.assertEqual("local:p-1", notes["field.cattle.io/projectId"])
        self.assertEqual("v1", notes["kubevirt.io/latest-observed-api-version"])
        chart = self.get(PRIVATE, "viewer")["object"]["metadata"]["annotations"]
        self.assertEqual("web", chart["homestead.io/group"])
        self.assertEqual("abc123", chart["objectset.rio.cattle.io/hash"])
        self.assertEqual("helm-controller", chart["helmcharts.cattle.io/managed-by"])

    def test_the_previous_values_kept_during_an_upgrade_are_masked(self):
        answer = self.get(PRIVATE, "viewer")
        self.assertEqual("••• (hidden)", answer["object"]["metadata"]["annotations"]["homestead.io/helm-previous"])
        self.assertHidden(answer)

    def test_the_prefix_rule(self):
        safe = RES._safe_annotation
        for key in ("deployment.kubernetes.io/revision", "kubernetes.io/description", "homestead.io/group",
                    "meta.helm.sh/release-name", "objectset.rio.cattle.io/id", "kube-vip.io/vipHost"):
            self.assertTrue(safe(key), key)
        for key in ("kubernetes.io/change-cause", "nginx.ingress.kubernetes.io/auth-url", "evil.k8s.io/x",
                    "anything.cattle.io/x", "secrets.cattle.io/x", "homestead.io/helm-previous",
                    "cdi.kubevirt.io/storage.import.extraHeaders", "longhorn.io/backup-token",
                    "kubevirt.io/client-password", "objectset.rio.cattle.io/applied", "example.com/x", "plain"):
            self.assertFalse(safe(key), key)


class Sizes(Base):
    def test_a_single_line_mask_says_no_length(self):
        chart = self.get(CHART, "viewer")["object"]
        self.assertEqual("••• (hidden)", chart["spec"]["chartContent"])
        self.assertEqual("••• (hidden)", chart["metadata"]["annotations"]["example.com/owner"])
        self.assertEqual("••• (hidden)", chart["metadata"]["annotations"]["objectset.rio.cattle.io/applied"])

    def test_line_counts_are_rounded(self):
        self.assertEqual("••• (hidden, under 10 lines)", self.get(CHART, "viewer")["object"]["spec"]["valuesContent"])
        self.assertEqual("••• (hidden, about 50 lines)", self.get(PRIVATE, "viewer")["object"]["spec"]["valuesContent"])
        for lines, said in ((1, "under 10 lines"), (9, "under 10 lines"), (10, "about 10 lines"), (14, "about 10 lines"),
                            (15, "about 20 lines"), (149, "about 100 lines"), (151, "about 200 lines"),
                            (1234, "about 1000 lines")):
            self.assertEqual(said, RES._about(lines), lines)

    def test_an_off_list_kinds_fields_say_no_exact_size(self):
        widget = self.get("/apis/widgets.example.com/v1/namespaces/lab/widgets/w1", "viewer")["object"]
        self.assertEqual("••• (hidden: needs the admin role, under 10 lines)", widget["spec"])
        machine = self.get("/apis/rke-machine-config.cattle.io/v1/namespaces/fleet-default/harvesterconfigs/pool1",
                           "viewer")["object"]
        self.assertEqual("••• (hidden: needs the admin role)", machine["cpuCount"])
        self.assertEqual("••• (hidden: needs the admin role, under 10 lines)", machine["userData"])

    def test_a_masked_map_keeps_only_its_own_keys(self):
        # Setting names show what a chart is configured with; anything below
        # them can be data (a map keyed by user name), so it is one mask.
        # A HelmRelease is off the safe list, so its spec is one note for a
        # viewer; the second layer is what any other reader of spec gets.
        for path, wanted in ((USERS, {"users": "•••", "replicas": "•••"}), (RELEASE, {"auth": "•••", "replicas": "•••"})):
            obj = self.c.objects[path]
            RES.hide_sensitive(obj, "helm.toolkit.fluxcd.io")
            self.assertEqual(wanted, obj["spec"]["values"])
            self.assertHidden(obj)
        self.assertEqual({"auth.adminPassword": "•••", "replicas": "•••"}, self.get(CHART, "viewer")["object"]["spec"]["set"])
        self.assertHidden(self.get(USERS, "viewer"))

    def test_an_admin_and_the_reveal_route_still_see_everything(self):
        for path in (USERS, PRIVATE, API, CHART):
            for answer in (self.get(path, "admin"), self.get(path, "admin", "/api/resources/reveal")):
                self.assertEqual(self.c.objects[path]["spec"] if "spec" in self.c.objects[path] else None,
                                 answer["object"].get("spec"))
                self.assertEqual(self.c.objects[path]["metadata"].get("annotations"),
                                 answer["object"]["metadata"].get("annotations"))

    def test_masking_twice_still_changes_nothing(self):
        once = self.get(PRIVATE, "viewer")["object"]
        again = copy.deepcopy(once)
        RES.hide_for_low_role(again, RES._kind("helm.cattle.io", "v1", "helmcharts"), "admin")
        self.assertEqual(once, again)


class Urls(Base):
    def test_strip(self):
        strip = MASKS.strip_url_secrets
        cases = {
            "https://deploy:hunter2@charts.example.com/stable": "https://•••@charts.example.com/stable",
            "oci://robot@registry.example.com/charts": "oci://•••@registry.example.com/charts",
            "https://x.example.com/a?token=abc&page=2#top": "https://x.example.com/a?token=•••&page=2#top",
            "https://x.example.com/a?X-Amz-Signature=abc&X-Amz-Credential=def": "https://x.example.com/a?X-Amz-Signature=•••&X-Amz-Credential=•••",
            "see https://u:p@a.example.com and git://b.example.com?api_key=k1": "see https://•••@a.example.com and git://b.example.com?api_key=•••",
            "--repo=https://u:p@h.example.com/x?sig=1&password=2&pwd=3&client_secret=4&access_token=5":
                "--repo=https://•••@h.example.com/x?sig=•••&password=•••&pwd=•••&client_secret=•••&access_token=•••",
            "https://example.com/start?page=2": "https://example.com/start?page=2",
            "https://example.com/path@v1/file": "https://example.com/path@v1/file",
            "user@example.com and a?token=x without a URL": "user@example.com and a?token=x without a URL",
            "": "",
        }
        for given, wanted in cases.items():
            self.assertEqual(wanted, strip(given), given)
            self.assertEqual(wanted, strip(strip(given)), "stripping twice changes nothing")

    def test_a_viewer_sees_urls_without_credentials(self):
        chart = self.get(PRIVATE, "viewer")["object"]["spec"]
        self.assertEqual("https://•••@charts.example.com/stable", chart["repo"])
        self.assertEqual("https://charts.example.com/app-1.0.tgz?token=•••&arch=amd64", chart["chart"])
        api = self.get(API, "viewer")["object"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(["--notify=https://hooks.example.com/post?sig=•••&room=ops"], api["args"])
        self.assertEqual("postgres://•••@db.example.com:5432/app", api["env"][0]["value"])
        self.assertEqual("https://example.com/start?page=2", api["env"][1]["value"])
        hook = self.get(WEBHOOK, "viewer")["object"]["webhooks"][0]["clientConfig"]["url"]
        self.assertEqual("https://hook.example.com/mutate?api_key=•••&X-Amz-Signature=•••", hook)
        for path in (PRIVATE, API, WEBHOOK):
            answer = self.get(path, "viewer")
            self.assertHidden(answer)
            self.assertTrue(answer["values_hidden"])

    def test_an_admin_and_reveal_see_the_full_urls(self):
        for answer in (self.get(PRIVATE, "admin"), self.get(PRIVATE, "admin", "/api/resources/reveal")):
            self.assertEqual("https://deploy:hunter2@charts.example.com/stable", answer["object"]["spec"]["repo"])
        self.assertIn("api_key=s3cr3t-t0ken", self.get(WEBHOOK, "admin")["object"]["webhooks"][0]["clientConfig"]["url"])

    def test_list_columns_lose_url_credentials_below_reveal(self):
        table = {"columnDefinitions": [{"name": "Name", "priority": 0}, {"name": "Repo", "priority": 0}],
                 "rows": [{"object": {"metadata": {"name": "private", "namespace": "kube-system"}},
                           "cells": ["private", "https://deploy:hunter2@charts.example.com/stable"]}]}
        with mock.patch.object(RES, "table_get", lambda path: copy.deepcopy(table)):
            viewer = self.get(PRIVATE, "viewer", "/api/resources/list")
            admin = self.get(PRIVATE, "admin", "/api/resources/list")
        self.assertEqual("https://•••@charts.example.com/stable", viewer["rows"][0]["cells"][1])
        self.assertHidden(viewer)
        self.assertEqual("https://deploy:hunter2@charts.example.com/stable", admin["rows"][0]["cells"][1])

    def test_saving_or_creating_a_copy_with_a_stripped_url_is_refused(self):
        group, version, resource, ns, name = _path_parts(DBURL)
        viewer = self.get(DBURL, "viewer")
        self.assertEqual("postgres://•••@db.example.com:5432/app",
                         viewer["object"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"])
        with self.assertRaisesRegex(ValueError, "hidden"):
            RES.save_object(group, version, resource, ns, name, viewer["yaml"])
        annotation_only = self.get(DBURL, "admin")["yaml"].replace("token=s3cr3t-t0ken", "token=•••")
        with self.assertRaisesRegex(ValueError, "hidden"):
            RES.save_object(group, version, resource, ns, name, annotation_only)
        with self.assertRaisesRegex(ValueError, "hidden"):
            RES.create_objects(viewer["yaml"].replace("name: dburl", "name: dburl-copy"))
        self.assertEqual([], self.c.sent)

    def test_an_admins_copy_with_full_urls_still_saves(self):
        group, version, resource, ns, name = _path_parts(DBURL)
        RES.save_object(group, version, resource, ns, name, self.get(DBURL, "admin")["yaml"])
        self.assertEqual("postgres://app:hunter2@db.example.com:5432/app",
                         self.c.sent[0][2]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"])

    def test_helms_release_page_strips_the_source_address_for_a_viewer(self):
        record = {"name": "demo", "namespace": "lab", "version": 1, "info": {}, "config": {},
                  "chart": {"metadata": {"name": "demo"}}}
        secret = {"data": {"release": base64.b64encode(base64.b64encode(gzip.compress(json.dumps(record).encode()))).decode()}}
        chart = {"metadata": {"name": "demo", "namespace": "lab"},
                 "spec": {"repo": "https://deploy:hunter2@charts.example.com", "chart": "https://charts.example.com/d.tgz?token=s3cr3t-t0ken"}}
        with mock.patch.object(HELM, "kget", return_value={"items": [secret]}), \
                mock.patch.object(HELM, "helmcharts", return_value=[chart]):
            public = HELM.release("lab", "demo", include_sensitive=False)
            private = HELM.release("lab", "demo", include_sensitive=True)
        self.assertEqual("https://•••@charts.example.com", public["source"]["repo"])
        self.assertEqual("https://charts.example.com/d.tgz?token=•••", public["source"]["chart"])
        self.assertHidden(public)
        self.assertEqual(chart["spec"]["repo"], private["source"]["repo"])
        self.assertEqual(chart["spec"]["chart"], private["source"]["chart"])


LOG_TEXT = "2026-10-28T00:00:00Z + helm install --set auth.adminPassword=hunter2\n"
INSTALL_POD = {"apiVersion": "v1", "kind": "Pod",
               "metadata": {"name": "helm-install-app-x1", "namespace": "kube-system", "uid": "p1",
                            "labels": {"helmcharts.helm.cattle.io/chart": "app", "job-name": "helm-install-app"},
                            "creationTimestamp": "2026-10-28T00:00:00Z"},
               "spec": {"containers": [{"name": "helm"}]}, "status": {"phase": "Succeeded"}}
# A pod the Job made whose labels were changed: its Job's name still tells.
DELETE_POD = {"apiVersion": "v1", "kind": "Pod",
              "metadata": {"name": "helm-delete-app-z9", "namespace": "kube-system", "uid": "p3",
                           "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": "helm-delete-app", "uid": "j2"}]},
              "spec": {"containers": [{"name": "helm"}]}, "status": {"phase": "Succeeded"}}
APP_POD = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": "web-1", "namespace": "lab", "uid": "p2", "labels": {"app": "web"}},
           "spec": {"containers": [{"name": "web"}]}, "status": {"phase": "Running"}}


class Logs(unittest.TestCase):
    def setUp(self):
        self.kube = Kube(copy.deepcopy(INSTALL_POD), copy.deepcopy(DELETE_POD), copy.deepcopy(APP_POD))
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        self.read = []

        def urlopen(request, **kwargs):
            self.read.append(request.full_url)
            return io.BytesIO(LOG_TEXT.encode())
        # server.py binds the workloads module to homestead_kube, which the fake answers.
        patch = mock.patch.object(WL.urllib.request, "urlopen", urlopen)
        patch.start()
        self.addCleanup(patch.stop)

    def logs(self, role, **query):
        handler = WL.ROUTES[("GET", "/api/logs")][1]
        return handler(ROUTER.Request("GET", "/api/logs", {k: [v] for k, v in query.items()}, {}, "someone", role))

    def test_a_viewer_cannot_read_a_helm_install_pods_log(self):
        for role in ("viewer", "operator", None):
            for query in ({"ns": "kube-system", "pod": "helm-install-app-x1"},
                          {"ns": "kube-system", "pod": "helm-install-app-x1", "container": "helm"},
                          {"ns": "kube-system", "job": "helm-install-app"},
                          {"ns": "kube-system", "pod": "helm-delete-app-z9", "container": "helm"}):
                with self.subTest(role=role, query=query):
                    answer = self.logs(role, **query)
                    self.assertEqual(403, answer.code)
                    self.assertNotIn("hunter2", repr(answer.body))
                    self.assertIn("admin", answer.body["error"])
        self.assertEqual([], self.read, "the log is never read for them")

    def test_a_pod_that_cannot_be_read_is_not_shown_below_reveal(self):
        answer = self.logs("viewer", ns="kube-system", pod="helm-install-gone", container="helm")
        self.assertEqual(409, answer.code)
        self.assertEqual([], self.read)

    def test_an_admin_still_reads_it(self):
        answer = self.logs("admin", ns="kube-system", pod="helm-install-app-x1")
        self.assertEqual(200, answer.code)
        self.assertIn("hunter2", answer.body)

    def test_an_ordinary_pods_log_is_unchanged_for_a_viewer(self):
        answer = self.logs("viewer", ns="lab", pod="web-1", container="web")
        self.assertEqual(200, answer.code)
        self.assertEqual(LOG_TEXT, answer.body)

    def test_an_agent_running_as_viewer_is_refused_too(self):
        # MCP reads logs through this route, under its key's role.
        with self.assertRaises(TOOLS.ToolError) as refused:
            TOOLS.call("GET", "/api/logs", {"ns": "kube-system", "pod": "helm-install-app-x1"}, user="api-key:agent", role="viewer")
        self.assertNotIn("hunter2", repr(refused.exception.args))
        self.assertEqual([], self.read)
        answer = TOOLS.call("GET", "/api/logs", {"ns": "kube-system", "pod": "helm-install-app-x1"}, user="api-key:agent", role="admin")
        self.assertIn("hunter2", repr(answer))


class JobLogs(unittest.TestCase):
    """The job tray's log of a Helm job reads the same pod."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = OPS.DATA_DIR, dict(OPS.LOGGERS), (JOBLOGS.kget, JOBLOGS.ktext)
        self.addCleanup(lambda: (setattr(OPS, "DATA_DIR", saved[0]), OPS.LOGGERS.clear(), OPS.LOGGERS.update(saved[1]),
                                 JOBLOGS.bind(*saved[2])))
        OPS.DATA_DIR = self.tmp.name
        pods = {"helm-install-app": INSTALL_POD, "web": APP_POD}

        def get(path):
            for job, pod in pods.items():
                if f"job-name%3D{job}" in path:
                    return {"items": [copy.deepcopy(pod)]}
            return {"items": []}
        JOBLOGS.bind(get, lambda path: LOG_TEXT)
        JOBLOGS.register(OPS)
        OPS.LOGGERS["plain"] = JOBLOGS._ref_job(title="Copy")
        self.helm = OPS.start("helm", "Helm install app", {}, "/", {"namespace": "kube-system", "name": "helm-install-app"})["id"]
        self.plain = OPS.start("plain", "Copy", {}, "/", {"namespace": "lab", "name": "web"})["id"]

    def log(self, op, role):
        handler = OPS.ROUTES[("GET", "/api/operations/log")][1]
        return handler(ROUTER.Request("GET", "/api/operations/log", {"id": [op]}, {}, "someone", role))

    def test_a_viewer_gets_a_note_instead_of_helms_output(self):
        for role in ("viewer", "operator", None):
            with self.subTest(role=role):
                answer = self.log(self.helm, role)
                self.assertNotIn("hunter2", repr(answer))
                self.assertEqual("", answer["sources"][0]["text"])
                self.assertIn("admin", answer["sources"][0]["note"])

    def test_an_admin_gets_it(self):
        self.assertIn("hunter2", self.log(self.helm, "admin")["sources"][0]["text"])

    def test_other_jobs_output_is_unchanged_for_a_viewer(self):
        self.assertEqual(LOG_TEXT, self.log(self.plain, "viewer")["sources"][0]["text"])


if __name__ == "__main__":
    unittest.main()
