#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线测试：命名 SeaTable Base 配置、路由与隔离。

测试只使用虚构 token/UUID，并通过 requests.get stub 断言认证和 metadata
不会跨 Base 复用；不会访问或修改线上 SeaTable。
"""
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from adapters.factory import get_adapter, get_adapters, get_base_config, load_config
from adapters.seatable import SeaTableAdapter


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class NamedBaseRoutingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "backend": "seatable",
            "seatable": {
                "default_base": "production",
                "bases": {
                    "production": {"api_token": "prod-token", "base_uuid": "prod-uuid"},
                    "tasks": {"api_token": "task-token", "base_uuid": "task-uuid"},
                },
            },
            "local": {"data_dir": "data"},
        }

    def test_named_config_resolution(self):
        prod = get_base_config(self.cfg, "production")
        tasks = get_base_config(self.cfg, "tasks")
        self.assertEqual(prod["base_uuid"], "prod-uuid")
        self.assertEqual(tasks["api_token"], "task-token")
        self.assertEqual(get_base_config(self.cfg)["name"], "production")
        self.assertIsNone(get_base_config(self.cfg, "missing"))

    def test_each_named_base_is_independent(self):
        prod = get_adapter(self.cfg, "production")
        tasks = get_adapter(self.cfg, "tasks")
        self.assertIsInstance(prod, SeaTableAdapter)
        self.assertIsInstance(tasks, SeaTableAdapter)
        self.assertEqual(prod.token, "prod-token")
        self.assertEqual(tasks.token, "task-token")
        self.assertEqual(prod.uuid, "prod-uuid")
        self.assertEqual(tasks.uuid, "task-uuid")
        self.assertIsNot(prod, tasks)

        def fake_get(url, headers=None, params=None, timeout=None):
            if url.endswith("app-access-token/"):
                token = headers["Authorization"].split()[-1]
                return _Response({"access_token": "access-" + token,
                                  "dtable_server": "https://gateway.example"})
            uuid = url.split("/dtables/")[1].split("/")[0]
            return _Response({"metadata": {"tables": [{"name": "T", "columns": []}],
                                         "links": [], "uuid": uuid}})

        with patch("adapters.seatable.requests.get", side_effect=fake_get) as req:
            prod.auth()
            tasks.auth()
            self.assertEqual(prod._access, "access-prod-token")
            self.assertEqual(tasks._access, "access-task-token")
            self.assertEqual(prod.get_metadata("T")["table_name"], "T")
            self.assertEqual(tasks.get_metadata("T")["table_name"], "T")
            urls = [c.args[0] for c in req.call_args_list]
            self.assertTrue(any("prod-uuid" in u for u in urls))
            self.assertTrue(any("task-uuid" in u for u in urls))

    def test_get_adapters_returns_named_instances(self):
        adapters = get_adapters(self.cfg)
        self.assertEqual(set(adapters), {"production", "tasks"})
        self.assertEqual(adapters["production"].base_name, "production")
        self.assertEqual(adapters["tasks"].base_name, "tasks")

    def test_legacy_flat_config_still_works(self):
        cfg = {"backend": "seatable", "seatable": {
            "api_token": "legacy-token", "base_uuid": "legacy-uuid",
        }}
        adapter = get_adapter(cfg)
        self.assertIsInstance(adapter, SeaTableAdapter)
        self.assertEqual(adapter.token, "legacy-token")
        self.assertEqual(adapter.uuid, "legacy-uuid")
        self.assertEqual(get_base_config(cfg)["name"], "default")

    def test_example_declares_required_named_bases(self):
        cfg = load_config(os.path.join(HERE, "config.yaml.example"))
        self.assertIn("production", cfg["seatable"]["bases"])
        self.assertIn("tasks", cfg["seatable"]["bases"])


if __name__ == "__main__":
    unittest.main()
