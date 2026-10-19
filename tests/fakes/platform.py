"""A plain cluster for tests whose code asks homestead_platform.detect() but
that set up no cluster for it to read (#620).

detect() used to answer a failed read with this - no KubeVirt, no Longhorn,
no load balancer - and those tests ran on that guess without saying so. It
now raises instead of guessing, so they state the platform they assume:

    def setUp(self):
        plain_platform(self)
"""
from unittest import mock

import homestead_platform

PLAIN = {"distribution": "kubernetes", "version": "", "harvester": False, "longhorn": False, "kubevirt": False,
         "cdi": False, "helm_controller": False, "multus": False, "metrics": False, "load_balancer": "",
         "servicelb": False, "vip_class": "", "vip_service_election": None, "vip_shared_lease": False,
         "control_plane": [], "arch": []}


def plain_platform(test, **overrides):
    patch = mock.patch.object(homestead_platform, "detect", lambda force=False: {**PLAIN, **overrides})
    patch.start()
    test.addCleanup(patch.stop)
