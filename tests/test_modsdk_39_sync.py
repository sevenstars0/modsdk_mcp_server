# -*- coding: utf-8 -*-

import json
import io
import os
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from modsdk_mcp.docs_reader import DocsReader, _find_guide_root
from tools.sync_official_docs import (
    API_39_NAMES,
    EVENT_39_NAMES,
    ORIGIN_GUI_39_VALUES,
    PYTHON_WHITELIST_REQUIRED,
    PYTHON_WHITELIST_URL,
    build_sync_plan,
    official_url,
    sync,
)


LAST_MODIFIED = "Mon, 03 Aug 2026 03:57:12 GMT"

API_DOCS = {
    "RemovePlayerAnimationFromState": ("接口/玩家/渲染", "客户端"),
    "GetBlockCollision": ("接口/世界/方块管理", "服务端"),
    "SetBiomeInfo": ("接口/世界/地图", "服务端"),
    "GetBiomeInfo": ("接口/世界/地图", "服务端"),
    "SetBiomeByPos": ("接口/世界/地图", "服务端"),
    "SetBiomeByPosList": ("接口/世界/地图", "服务端"),
    "SetBiomeByVolume": ("接口/世界/地图", "服务端"),
    "SetPlayerMovable": ("接口/玩家/行为", "客户端"),
    "StopCustomMusicById": ("接口/音效", "客户端"),
    "GetScrollViewContentPath": ("接口/自定义UI/UI控件", "客户端"),
}

EVENT_DOCS = {
    "EntityEffectDamageServerEvent": ("事件/实体", "服务端"),
    "PlayerSleepServerEvent": ("事件/玩家", "服务端"),
    "PlayerStopSleepServerEvent": ("事件/玩家", "服务端"),
    "ClientChestOpenEvent": ("事件/UI", "客户端"),
    "ClientChestCloseEvent": ("事件/UI", "客户端"),
    "PlayerPermissionChangeClientEvent": ("事件/玩家", "客户端"),
}

PARAMS = {
    "RemovePlayerAnimationFromState": [
        "animationControllerName", "stateName", "animationName",
    ],
    "GetBlockCollision:服务端": ["pos", "dimensionId", "getAll"],
    "GetBlockCollision:客户端": ["pos", "getAll"],
    "PlayerSleepServerEvent": [
        "playerId", "fullName", "auxData", "dimensionid", "x", "y", "z",
    ],
    "PlayerStopSleepServerEvent": [
        "playerId", "fullName", "auxData", "dimensionid", "x", "y", "z",
    ],
    "ClientChestOpenEvent": [
        "playerId", "x", "y", "z", "fullName", "auxData", "dimensionId", "isLargeChest",
    ],
    "ClientChestCloseEvent": [
        "playerId", "x", "y", "z", "fullName", "auxData", "dimensionId", "isLargeChest",
    ],
}


def wrap_html(body):
    return '<html><main><div class="theme-default-content content__default">{}</div><footer></footer></main></html>'.format(body)


def href_for(doc_path, name=""):
    encoded = "/".join(urllib.parse.quote(part, safe="") for part in doc_path.split("/"))
    href = "/dev/mcmanual/mc-dev/mcdocs/1-ModAPI/{}.html".format(encoded)
    return href + ("#" + name.lower() if name else "")


def params_table(names):
    rows = "".join(
        "<tr><td>{0}</td><td>str</td><td>{0} 参数</td></tr>".format(name)
        for name in names
    )
    return (
        "<p>参数</p><table><thead><tr><th>参数名</th><th>数据类型</th><th>说明</th></tr></thead>"
        "<tbody>{}</tbody></table>"
    ).format(rows)


def entry_body(name, side, params, class_path=""):
    method = "<p>method in {}</p>".format(class_path) if class_path else ""
    return (
        "{method}<ul><li><p>描述</p><p>{name} 的官方描述</p></li>"
        "<li>{params}</li><li><p>返回值</p><p>无</p></li>"
        "<li><p>备注</p><ul><li>{name} 的官方备注</li></ul></li>"
        "<li><p>示例</p></li></ul>"
        "<pre class=\"language-python\"><code>result = comp.{name}()</code></pre>"
    ).format(method=method, name=name, params=params_table(params))


def api_section(name, side, params):
    class_side = "server" if side == "服务端" else "client"
    class_path = "mod.{}.component.fixture.{}Component{}".format(
        class_side,
        name,
        "Server" if side == "服务端" else "Client",
    )
    return (
        '<h2 id="{anchor}">{name}</h2><p><span>{side}</span></p>{body}'
    ).format(
        anchor=name.lower(),
        name=name,
        side=side,
        body=entry_body(name, side, params, class_path),
    )


def event_section(name, side, params):
    return (
        '<h2 id="{anchor}">{name}</h2><p><span>{side}</span></p>{body}'
    ).format(
        anchor=name.lower(),
        name=name,
        side=side,
        body=entry_body(name, side, params),
    )


def get_block_collision_section():
    server_body = entry_body(
        "GetBlockCollision",
        "服务端",
        PARAMS["GetBlockCollision:服务端"],
        "mod.server.component.blockInfoCompServer.BlockInfoComponentServer",
    )
    client_body = entry_body(
        "GetBlockCollision",
        "客户端",
        PARAMS["GetBlockCollision:客户端"],
        "mod.client.component.blockInfoCompClient.BlockInfoComponentClient",
    )
    return (
        '<h2 id="getblockcollision">GetBlockCollision</h2>'
        '<p><span>服务端</span><span>客户端</span></p>'
        '<h3 id="server">服务端接口</h3>{}'
        '<h3 id="client">客户端接口</h3>{}'
    ).format(server_body, client_body)


def index_html(rows):
    body = ["<h1>索引</h1><table><tr><th>名称</th><th>端</th><th>描述</th></tr>"]
    for name, doc_path, side in rows:
        body.append(
            '<tr><td><a href="{}">{}</a></td><td>{}</td><td>fixture 描述</td></tr>'.format(
                href_for(doc_path, name), name, side,
            )
        )
    body.append("</table>")
    return wrap_html("".join(body))


def build_fixture_pages(include_all_enum_values=True):
    pages = {}
    links = []
    for name, (doc_path, _) in API_DOCS.items():
        links.append('<a href="{}">{}</a>'.format(href_for(doc_path, name), name))
    for name, (doc_path, _) in EVENT_DOCS.items():
        links.append('<a href="{}">{}</a>'.format(href_for(doc_path, name), name))
    links.append('<a href="{}">OriginGUIName</a>'.format(href_for("枚举值/OriginGUIName")))
    pages[official_url("更新信息/3.9")] = wrap_html("<h1>3.9</h1>" + "".join(links))

    api_rows = []
    api_pages = {}
    for name, (doc_path, side) in API_DOCS.items():
        if name == "GetBlockCollision":
            api_rows.append((name, doc_path, "服务端"))
            api_rows.append((name, doc_path, "客户端"))
            api_pages.setdefault(doc_path, []).append(get_block_collision_section())
            continue
        api_rows.append((name, doc_path, side))
        api_pages.setdefault(doc_path, []).append(api_section(name, side, PARAMS.get(name, ["value"])))
    pages[official_url("接口/Api索引表")] = index_html(api_rows)
    for doc_path, sections in api_pages.items():
        pages[official_url(doc_path)] = wrap_html("".join(sections))

    event_rows = []
    event_pages = {}
    for name, (doc_path, side) in EVENT_DOCS.items():
        event_rows.append((name, doc_path, side))
        event_pages.setdefault(doc_path, []).append(event_section(name, side, PARAMS.get(name, ["entityId"])))
    pages[official_url("事件/事件索引表")] = index_html(event_rows)
    for doc_path, sections in event_pages.items():
        pages[official_url(doc_path)] = wrap_html("".join(sections))

    enum_values = ["LegacyValue{}".format(index) for index in range(5)]
    enum_values.extend(sorted(ORIGIN_GUI_39_VALUES))
    if not include_all_enum_values:
        enum_values.remove("VoiceTrans")
    enum_code = "\n".join('{} = "binding.area.{}"'.format(name, name.lower()) for name in enum_values)
    pages[official_url("枚举值/OriginGUIName")] = wrap_html(
        '<h1>OriginGUIName</h1><pre class="language-python"><code>class OriginGUIName(object):\n{}</code></pre>'.format(
            "\n".join("    " + line for line in enum_code.splitlines())
        )
    )

    modules = sorted(PYTHON_WHITELIST_REQUIRED)
    modules.append("ast（3.3新增）")
    modules.extend("allowed.module{:03d}".format(index) for index in range(456 - len(modules)))
    cells = "".join("<td>{}</td>".format(module) for module in modules)
    pages[PYTHON_WHITELIST_URL] = wrap_html("<h1>Python模块白名单</h1><table><tr>{}</tr></table>".format(cells))
    return pages


class FixtureFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def __call__(self, url):
        self.requests.append(url)
        if url not in self.pages:
            raise AssertionError("测试禁止网络且不存在 fixture URL: {}".format(url))
        return self.pages[url], {"Last-Modified": LAST_MODIFIED}


def write_repo_seed(repo_root):
    docs = repo_root / "docs"
    docs.mkdir(parents=True)
    interface = {
        "legacy.ClientBlock": [{
            "name": "GetBlockCollision",
            "path": "legacy.ClientBlock",
            "desc": "旧描述",
            "doc_class_path": ["旧分类"],
            "param": [],
            "return": {},
            "state": [{"version": "3.8", "operation": "新增", "comment": "旧状态"}],
            "side": "客户端",
            "custom_marker": "必须保留",
        }],
    }
    events = {
        "server.serverEvent": [{
            "name": "PlayerSleepServerEvent",
            "path": "server.serverEvent",
            "desc": "旧睡眠事件",
            "doc_class_path": ["玩家"],
            "param": [],
            "return": {},
            "state": [{"version": "3.8", "operation": "新增", "comment": "旧事件状态"}],
            "side": "服务端",
            "custom_event_marker": True,
        }],
    }
    config = {
        "title": "ModAPI",
        "children": [{
            "title": "更新信息",
            "children": ["/mcdocs/1-ModAPI/更新信息/3.6"],
        }],
    }
    (docs / "interface.json").write_text(json.dumps(interface, ensure_ascii=False), encoding="utf-8")
    (docs / "events.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")


def tree_snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def all_entries(data):
    return [entry for entries in data.values() for entry in entries]


class ModSdk39SyncTest(unittest.TestCase):
    def make_repo(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        write_repo_seed(root)
        return temporary, root

    def test_offline_plan_discovers_and_validates_complete_contract(self):
        temporary, root = self.make_repo()
        self.addCleanup(temporary.cleanup)
        pages = build_fixture_pages()
        fetcher = FixtureFetcher(pages)

        plan = build_sync_plan(
            root,
            version="3.9",
            include_python_whitelist=True,
            fetcher=fetcher,
        )

        self.assertEqual(plan.target_names, API_39_NAMES | EVENT_39_NAMES | {"OriginGUIName"})
        self.assertEqual(len(plan.api_entries), 11)
        self.assertEqual(len(plan.event_entries), 6)
        self.assertEqual(len(plan.target_names), 17)
        self.assertEqual(len(plan.python_whitelist_modules), 456)
        self.assertEqual(len(plan.python_whitelist_entries), 456)
        snapshot_path = root / "standard" / "registry" / "snapshots" / "python-module-whitelist.json"
        snapshot = json.loads(plan.files[snapshot_path])
        self.assertEqual(snapshot["module_count"], 456)
        self.assertEqual(
            [entry["normalized_name"] for entry in snapshot["entries"]],
            snapshot["modules"],
        )
        ast_entry = next(
            entry for entry in snapshot["entries"] if entry["normalized_name"] == "ast"
        )
        self.assertEqual(ast_entry["raw_display"], "ast（3.3新增）")
        self.assertEqual(snapshot["last_modified"], "2026-08-03T03:57:12Z")
        self.assertEqual(set(fetcher.requests), set(pages))
        self.assertEqual(len(plan.discovered_doc_paths), 13)

        block_entries = [entry for entry in plan.api_entries if entry["name"] == "GetBlockCollision"]
        block_params = {
            entry["side"]: [param["param_name"] for param in entry["param"]]
            for entry in block_entries
        }
        self.assertEqual(block_params["服务端"], ["pos", "dimensionId", "getAll"])
        self.assertEqual(block_params["客户端"], ["pos", "getAll"])
        animation = next(entry for entry in plan.api_entries if entry["name"] == "RemovePlayerAnimationFromState")
        self.assertEqual(
            [param["param_name"] for param in animation["param"]],
            ["animationControllerName", "stateName", "animationName"],
        )
        self.assertTrue(animation["remarks"])
        self.assertTrue(animation["examples"])
        self.assertEqual(animation["source_last_modified"], LAST_MODIFIED)

    def test_default_guide_root_is_local_and_diagnostics_use_stderr(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                guide_root = _find_guide_root()

        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(Path(guide_root), Path(__file__).resolve().parents[1] / "input")
        self.assertIn("官方教程文档", stderr.getvalue())
        self.assertNotIn(str(Path(__file__).resolve().parents[1]), stderr.getvalue())

    def test_dry_run_has_no_filesystem_side_effects(self):
        temporary, root = self.make_repo()
        self.addCleanup(temporary.cleanup)
        before = tree_snapshot(root)

        sync(
            root,
            version="3.9",
            dry_run=True,
            delay=0,
            include_python_whitelist=True,
            fetcher=FixtureFetcher(build_fixture_pages()),
        )

        self.assertEqual(tree_snapshot(root), before)

    def test_validation_failure_writes_nothing(self):
        temporary, root = self.make_repo()
        self.addCleanup(temporary.cleanup)
        before = tree_snapshot(root)

        with self.assertRaisesRegex(ValueError, "OriginGUIName"):
            sync(
                root,
                version="3.9",
                dry_run=False,
                delay=0,
                fetcher=FixtureFetcher(build_fixture_pages(include_all_enum_values=False)),
            )

        self.assertEqual(tree_snapshot(root), before)

    def test_sync_preserves_history_unknown_fields_and_reader_exposes_provenance(self):
        temporary, root = self.make_repo()
        self.addCleanup(temporary.cleanup)
        sync(
            root,
            version="3.9",
            dry_run=False,
            delay=0,
            include_python_whitelist=True,
            fetcher=FixtureFetcher(build_fixture_pages()),
        )

        interface = json.loads((root / "docs" / "interface.json").read_text(encoding="utf-8"))
        client_block = next(
            entry
            for entry in all_entries(interface)
            if entry["name"] == "GetBlockCollision" and entry["side"] == "客户端"
        )
        self.assertEqual(client_block["custom_marker"], "必须保留")
        self.assertEqual([state["version"] for state in client_block["state"]], ["3.8", "3.9"])

        events = json.loads((root / "docs" / "events.json").read_text(encoding="utf-8"))
        sleep = next(entry for entry in all_entries(events) if entry["name"] == "PlayerSleepServerEvent")
        self.assertTrue(sleep["custom_event_marker"])
        self.assertEqual([state["version"] for state in sleep["state"]], ["3.8", "3.9"])

        config = json.loads((root / "docs" / "config.json").read_text(encoding="utf-8"))
        versions = config["children"][0]["children"]
        self.assertEqual(versions[:2], [
            "/mcdocs/1-ModAPI/更新信息/3.9",
            "/mcdocs/1-ModAPI/更新信息/3.8",
        ])

        reader = DocsReader(str(root / "docs"))
        reader.load_all_docs()
        detail = reader.get_api_detail("RemovePlayerAnimationFromState")
        self.assertEqual(detail["source_last_modified"], LAST_MODIFIED)
        self.assertTrue(detail["source_url"].endswith("#removeplayeranimationfromstate"))
        self.assertTrue(detail["remarks"])
        self.assertTrue(detail["examples"])
        self.assertEqual(detail["state"][-1]["version"], "3.9")
        self.assertIn("VoiceTrans", reader.get_enum_inline("OriginGUIName"))
        search_result = reader.search_api("官方备注", limit=20)
        self.assertTrue(search_result)
        self.assertIn("source_url", search_result[0])

        whitelist = (root / "docs" / "审核与下架" / "Python模块白名单.md").read_text(encoding="utf-8")
        self.assertIn("456 个唯一模块", whitelist)
        self.assertIn("`mod.server.extraServerApi`", whitelist)
        self.assertNotIn("| `os` |", whitelist)

        snapshot = json.loads((
            root / "standard" / "registry" / "snapshots" / "python-module-whitelist.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(len(snapshot["entries"]), 456)
        self.assertEqual(snapshot["entries"][0]["raw_display"], snapshot["modules"][0])

    def test_atomic_replace_rolls_back_after_mid_commit_failure(self):
        temporary, root = self.make_repo()
        self.addCleanup(temporary.cleanup)
        before = tree_snapshot(root)
        real_replace = os.replace
        calls = {"count": 0}

        def flaky_replace(source, target):
            calls["count"] += 1
            if calls["count"] == 3:
                raise OSError("fixture replacement failure")
            return real_replace(source, target)

        with mock.patch("tools.sync_official_docs.os.replace", side_effect=flaky_replace):
            with self.assertRaisesRegex(OSError, "fixture replacement failure"):
                sync(
                    root,
                    version="3.9",
                    dry_run=False,
                    delay=0,
                    fetcher=FixtureFetcher(build_fixture_pages()),
                )

        self.assertEqual(tree_snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
