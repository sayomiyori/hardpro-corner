"""Regression tests for saved layouts and guarded GPU profile transitions."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import json
import settings

EXAMPLE = json.loads(Path(__file__).with_name("settings.example.json").read_bytes())
with patch.object(settings, "settings", return_value=EXAMPLE):
    import gpu_profiles as gpu

WORK = ["manager", "coder", "comfy"]
FLASH = "Qwen3.8-Flash-Next-16K-3GPU"


class NcclProfileTest(unittest.TestCase):
    def test_guest_profile_requires_the_registered_loopback_endpoint(self):
        host = object.__new__(gpu.Host)
        name = FLASH + "-NCCL"
        p = dict(backend="external", gpus=[0, 1, 2], alias=name,
                 port=19161, base="http://127.0.0.1:19161")
        host.profiles = {name: p}
        self.assertEqual(host.profile(name), p)
        for changed in (dict(port=19163), dict(alias=FLASH), dict(gpus=[0, 2]),
                        dict(base="http://example.invalid")):
            with self.subTest(changed=changed), self.assertRaises(gpu.Refused):
                host.profiles[name] = p | changed
                host.profile(name)

    def test_work_layout_releases_an_empty_guest_before_starting_host_models(self):
        host = TestHost()
        host.running.clear()
        observe = host.observe
        guest_active = True

        def snapshot():
            return observe() | {"vm": guest_active}

        def release(args, timeout):
            nonlocal guest_active
            self.assertEqual(args, ["systemctl", "--user", "stop", gpu.VM_UNIT])
            self.assertFalse(host.running)
            guest_active = False

        host.observe = snapshot
        with patch.object(gpu, "cmd", side_effect=release) as stop:
            gpu.transition(host, set(WORK), Mock())
        stop.assert_called_once()
        self.assertEqual(host.events, [("start", n) for n in WORK])


class TestHost:
    def __init__(self):
        self.running = dict(zip(WORK, [42, 43, 44]))
        self.events = []
        self.reg = Mock()
        self.reg.catalog.return_value = {"roles": {}}
        self.busy = None

    def observe(self):
        owners = {
            name: [WORK.index(name)] if name in WORK else [0, 1, 2]
            for name in self.running
        }
        return {
            "managed": self.running.copy(),
            "owners": owners,
            "system": [],
            "cards": {str(i): {"used": 2, "i": i} for i in range(3)},
        }

    def profile(self, name):
        return {"model_path": __file__, "port": 8090}

    def ready(self, name):
        assert name in self.running

    def idle(self, name):
        if self.busy == name:
            raise gpu.Refused("Очередь занята")

    def stop(self, name, pid):
        assert self.running.pop(name) == pid
        self.events.append(("stop", name))

    def start(self, name):
        self.running[name] = 100 + len(self.events)
        self.events.append(("start", name))


class ProfileTests(unittest.TestCase):
    def test_saved_assignments(self):
        self.assertEqual(gpu.desired(WORK, FLASH), set(WORK))
        self.assertEqual(gpu.desired(["reserve", "coder", "idle"], FLASH), {"coder"})
        for recipe in gpu.FLASH:
            self.assertEqual(gpu.desired(["flash"] * 3, recipe), {recipe})
        self.assertEqual(gpu.desired(["video"] * 3, FLASH), set())
        for layout in [
            ["flash", "coder", "idle"],
            ["coder", "manager", "comfy"],
            ["3d"] * 3,
        ]:
            with self.assertRaises(gpu.Refused):
                gpu.desired(layout, FLASH)

    def test_cycle_and_repeated_working_profile(self):
        host = TestHost()
        for target in [{FLASH}, set(), set(WORK), set(WORK)]:
            proposed = gpu.plan(host, target)
            gpu.transition(host, target, Mock(), proposed["signature"])
            self.assertEqual(set(host.running), target)
        self.assertEqual(
            host.events,
            [("stop", n) for n in reversed(WORK)]
            + [("start", FLASH), ("stop", FLASH)]
            + [("start", n) for n in WORK],
        )

    def test_busy_queue_or_process_change_aborts_before_stop(self):
        host = TestHost()
        signature = gpu.plan(host, set())["signature"]
        host.busy = "comfy"
        with self.assertRaises(gpu.Refused):
            gpu.transition(host, set(), Mock(), signature)
        host.busy = None
        host.running["manager"] += 1
        with self.assertRaises(gpu.Refused):
            gpu.transition(host, set(), Mock(), signature)
        self.assertFalse(host.events)

    def test_failed_launch_restores_working_layout(self):
        host = TestHost()

        def start(name):
            if name == FLASH:
                raise gpu.Refused("Не удалось загрузить веса")
            TestHost.start(host, name)

        host.start = start
        job = {"target": [FLASH], "signature": gpu.plan(host, {FLASH})["signature"]}
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(gpu, "ROOT", Path(folder)),
            patch.object(gpu, "Host", return_value=host),
        ):
            gpu.run_job(job)
            self.assertEqual(gpu.read_job()["status"], "rolled_back")
            self.assertEqual(set(host.running), set(WORK))

    def test_stale_plan_never_rolls_back_new_workloads(self):
        host = TestHost()
        job = {"target": [], "signature": gpu.plan(host, set())["signature"]}
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(gpu, "ROOT", Path(folder)),
            patch.object(gpu, "Host", return_value=host),
            patch.object(gpu, "fingerprint", side_effect=[job["signature"], "changed"]),
        ):
            gpu.run_job(job)
            self.assertEqual(gpu.read_job()["status"], "blocked")
            self.assertFalse(host.events)

    def test_routes_restored_after_switch_to_another_flash_context(self):
        host = TestHost()
        roles = {
            "agent": gpu.CODER,
            "chat": gpu.CONFIG["manager_model"],
            "other": "independent",
        }
        original = roles.copy()
        host.reg.catalog.return_value = {"roles": roles}
        host.reg.set_role.side_effect = roles.__setitem__
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(gpu, "ROOT", Path(folder)),
        ):
            gpu.activate_routes(host, {FLASH}, {})
            self.assertEqual(roles["agent"], FLASH)
            gpu.activate_routes(host, {"Qwen3.8-Flash-Next-64K-3GPU"}, {})
            gpu.activate_routes(host, set(WORK), {})
            self.assertEqual(roles, original)
