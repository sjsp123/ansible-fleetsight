#!/usr/bin/env python3
"""Local-only regression tests for timesync variable resolution and validation.

Run: python3 -m unittest discover -s rhel_system_roles/tests -v
Requires ansible-playbook and PyYAML in the test environment. These tests
run only assert/set_fact/debug tasks from the playbook in temporary fixtures.
They never invoke the system role, gather facts, contact managed hosts,
install packages, or change chrony.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
UNSET = object()
BOUNDARY = "TIMESYNC_ROLE_BOUNDARY_REACHED"


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is required")
class TimesyncConfigurationTest(unittest.TestCase):
    def run_fixture(self, global_servers=UNSET, realm_servers=None,
                    expected=None, repeat=False, direct_override=UNSET,
                    realm_one_ini=None):
        realm_servers = realm_servers or {}
        expected = expected or {}
        source = yaml.safe_load(
            (ROOT / "playbooks/deploy_timesync_role.yml").read_text()
        )[0]
        tasks = source["tasks"]
        start = next(i for i, task in enumerate(tasks)
                     if task["name"] == "Validate the effective NTP server list")
        stop = next(i for i, task in enumerate(tasks)
                    if task.get("ansible.builtin.include_role"))
        selected = tasks[start:stop]
        selected = selected + selected if repeat else selected
        selected = selected + [{
            "name": "Check each host's resolved role-format list",
            "ansible.builtin.assert": {
                "that": [
                    "_timesync_role_servers | map(attribute='hostname') | list == expected_servers",
                    "_timesync_role_servers | map(attribute='iburst') | unique | list == [true]",
                ],
            },
        }, {
            "name": "Report reaching the role boundary without running it",
            "ansible.builtin.debug": {"msg": BOUNDARY},
        }]
        play = {
            "hosts": "all",
            "gather_facts": False,
            "any_errors_fatal": source["any_errors_fatal"],
            "tasks": selected,
        }
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)

            def write_yaml(relative, data):
                path = directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(yaml.safe_dump(data))

            (directory / "ansible.cfg").write_text(
                "[defaults]\nretry_files_enabled=False\n"
            )
            inventory = (
                "[realm_one]\nnode_one ansible_connection=local\n\n"
                "[realm_two]\nnode_two ansible_connection=local\n\n"
                "[ungrouped]\nnode_default ansible_connection=local\n"
            )
            if realm_one_ini is not None:
                inventory += "\n[realm_one:vars]\nchrony_ntp_servers=" + realm_one_ini + "\n"
            (directory / "inventory").write_text(inventory)
            write_yaml("group_vars/all/timesync.yml", yaml.safe_load(
                (ROOT / "group_vars/all/timesync.yml").read_text()
            ))
            if global_servers is not UNSET:
                write_yaml("group_vars/all/site_hosts.yml",
                           {"chrony_ntp_servers": global_servers})
            for group, servers in realm_servers.items():
                write_yaml("group_vars/" + group + "/site_hosts.yml",
                           {"chrony_ntp_servers": servers})
            for host in ("node_one", "node_two", "node_default"):
                values = {"expected_servers": expected.get(host, ["default.example.internal"])}
                if direct_override is not UNSET:
                    values["timesync_ntp_hostnames"] = direct_override
                write_yaml("host_vars/" + host + ".yml", values)
            write_yaml("probe.yml", [play])
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("ANSIBLE_")}
            env.update({
                "ANSIBLE_CONFIG": str(directory / "ansible.cfg"),
                "ANSIBLE_NOCOLOR": "1",
            })
            result = subprocess.run(
                ["ansible-playbook", "-i", "inventory", "probe.yml"],
                cwd=str(directory), env=env, text=True, capture_output=True,
                timeout=60,
            )
            return result.returncode, result.stdout + result.stderr

    def test_two_realms_override_global_without_cross_host_leakage(self):
        code, output = self.run_fixture(
            global_servers=["default.example.internal"],
            realm_servers={
                "realm_one": ["time.corp.example.com", "time1.corp.example.com"],
                "realm_two": ["time.other.example.com"],
            },
            expected={
                "node_one": ["time.corp.example.com", "time1.corp.example.com"],
                "node_two": ["time.other.example.com"],
            },
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count('"msg": "' + BOUNDARY + '"'), 3)

    def test_global_only_configuration_is_unchanged(self):
        code, output = self.run_fixture(global_servers=["default.example.internal"])
        self.assertEqual(code, 0, output)

    def test_existing_public_fallback_is_unchanged(self):
        expected = {host: ["0.rhel.pool.ntp.org", "1.rhel.pool.ntp.org"]
                    for host in ("node_one", "node_two", "node_default")}
        code, output = self.run_fixture(expected=expected)
        self.assertEqual(code, 0, output)

    def test_explicit_timesync_override_is_preserved(self):
        expected = {host: ["override.example.internal"]
                    for host in ("node_one", "node_two", "node_default")}
        code, output = self.run_fixture(
            global_servers=["default.example.internal"],
            direct_override=["override.example.internal"], expected=expected,
        )
        self.assertEqual(code, 0, output)

    def test_repeated_preparation_does_not_accumulate_servers(self):
        code, output = self.run_fixture(
            global_servers=["default.example.internal"], repeat=True,
        )
        self.assertEqual(code, 0, output)

    def test_invalid_lists_fail_before_any_host_reaches_role(self):
        cases = [
            [], "", "one.example.com,two.example.com",
            {"hostname": "one.example.com"}, None, True, 123,
            [""], ["   "], ["one.example.com two.example.com"],
            ["one.example.com,two.example.com"], [123], [None],
            ["one.example.com\n"], ["one.example.com\ttwo.example.com"],
            [{"hostname": "one.example.com"}],
        ]
        for value in cases:
            with self.subTest(value=value):
                code, output = self.run_fixture(
                    global_servers=["default.example.internal"],
                    realm_servers={"realm_one": value},
                )
                self.assertNotEqual(code, 0, output)
                self.assertNotIn('"msg": "' + BOUNDARY + '"', output)
                self.assertIn("NTP server", output)

    def test_old_inline_string_fails_without_global_default(self):
        code, output = self.run_fixture(
            realm_one_ini="one.example.com,two.example.com",
        )
        self.assertNotEqual(code, 0, output)
        self.assertIn("nonempty YAML list", output)
        self.assertNotIn('"msg": "' + BOUNDARY + '"', output)

    def test_yaml_realm_override_wins_over_stale_inline_value(self):
        code, output = self.run_fixture(
            global_servers=["default.example.internal"],
            realm_servers={"realm_one": ["correct.example.internal"]},
            realm_one_ini="wrong.example.com,wrong2.example.com",
            expected={"node_one": ["correct.example.internal"]},
        )
        self.assertEqual(code, 0, output)

    def test_hostname_ipv4_and_ipv6_entries_are_accepted(self):
        servers = ["ntp.example.internal", "192.0.2.10", "2001:db8::10"]
        expected = {host: servers for host in ("node_one", "node_two", "node_default")}
        code, output = self.run_fixture(global_servers=servers, expected=expected)
        self.assertEqual(code, 0, output)


if __name__ == "__main__":
    unittest.main()
