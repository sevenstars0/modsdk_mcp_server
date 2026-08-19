#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将网易 ModSDK 官方文档同步到本地只读知识库。

同步过程先抓取、解析并验证全部内容，再以同目录临时文件统一替换目标文件。
MCP 运行时只读取本地文件，不会触发这里的网络请求。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


BASE_URL = "https://mc.163.com/dev/mcmanual/mc-dev/mcdocs/1-ModAPI"
SITE_URL = "https://mc.163.com"
PYTHON_WHITELIST_URL = (
    "https://mc.163.com/dev/mcmanual/mc-dev/mcguide/"
    "36-%E5%AE%A1%E6%A0%B8%E4%B8%8E%E4%B8%8B%E6%9E%B6/"
    "%E8%AF%BE%E7%A8%8B07-Python%E6%A8%A1%E5%9D%97%E7%99%BD%E5%90%8D%E5%8D%95.html"
)
PYTHON_WHITELIST_DOC_PATH = "审核与下架/Python模块白名单"
USER_AGENT = "Mozilla/5.0 (compatible; modsdk-mcp-doc-sync/2.0)"

API_INDEX_PATH = "接口/Api索引表"
EVENT_INDEX_PATH = "事件/事件索引表"

API_39_NAMES = {
    "RemovePlayerAnimationFromState",
    "GetBlockCollision",
    "SetBiomeInfo",
    "GetBiomeInfo",
    "SetBiomeByPos",
    "SetBiomeByPosList",
    "SetBiomeByVolume",
    "SetPlayerMovable",
    "StopCustomMusicById",
    "GetScrollViewContentPath",
}

EVENT_39_NAMES = {
    "EntityEffectDamageServerEvent",
    "PlayerSleepServerEvent",
    "PlayerStopSleepServerEvent",
    "ClientChestOpenEvent",
    "ClientChestCloseEvent",
    "PlayerPermissionChangeClientEvent",
}

ENUM_39_NAMES = {"OriginGUIName"}

ORIGIN_GUI_39_VALUES = {
    "WalkState",
    "MobEffects",
    "Emote",
    "TurnInteract",
    "DpadNoTurnInteract",
    "GuiPassthrough",
    "MoveUpInvisible",
    "MiddleRight",
    "CodeBuilder",
    "MoveUpLeft",
    "MoveUpRight",
    "PaddleRight",
    "PaddleLeft",
    "Toast",
    "KeyJoy",
    "SneakJK",
    "Store",
    "VState0",
    "VoiceTrans",
}

PYTHON_WHITELIST_REQUIRED = {
    "__future__",
    "threading",
    "mod.server.extraServerApi",
    "mod.client.extraClientApi",
    "mod.common.mod",
}
PYTHON_WHITELIST_FORBIDDEN = {"os", "sys", "inspect", "importlib"}

# 3.8 更新页未稳定列出全部迁移项，保留版本级补充名称以兼容历史同步。
EXTRA_API_NAMES_BY_VERSION = {
    "3.8": {
        "AddModifier", "UpdateModifier", "RemoveModifier", "HasModifier",
        "GetAllModifiers", "SetFishingLineMax", "GetFishingLineMax",
        "SetFishingLineColor", "GetFishingLineColor", "UseItemToPos",
        "GetPlayerFishHookEntity", "GetPlayerFishItem", "GetPlayerIsFishing",
        "AddCapsuleGeometry", "AddSphereGeometry", "AddBoxTrigger",
        "AddForceAtPosLocal", "AddForceAtPos", "GetQueryableBoneOrientation",
        "ResetEntityExtraSkin", "ResetCameraPos", "BindItemToMinecraftModel",
        "BindItemToSkeletonModel", "SetBindBoneForBindItem",
        "GetBindBoneForBindItem", "SetBindItemRotation", "SetBindItemOffset",
        "SetBindItemScale", "GetBindItemRotation", "GetBindItemOffset",
        "GetBindItemScale", "PlayerDestroyBlock", "SetShearsDestroyBlockSpeed",
        "CancelShearsDestroyBlockSpeed", "CancelShearsDestroyBlockSpeedAll",
        "SetCameraPos", "SetToggleOption", "GetCarriedItem", "GetPlayerItem",
        "GetPlayerAllItems", "GetEntityItem", "GetContainerItem",
        "GetEnderChestItem", "SetUseLocalTime", "UseItemToEntity",
        "HideNeteaseStoreGui", "OpenNeteaseStoreGui",
    }
}

EXTRA_EVENT_NAMES_BY_VERSION = {
    "3.8": {
        "PlayerRemoveCustomContainerItemServerEvent",
        "PlayerAddCustomContainerItemServerEvent",
        "LiquidClippedServerEvent",
        "PhysxTriggerServerEvent",
        "PlayerFishingServerEvent",
        "PlayerFishingAfterServerEvent",
        "PlayerStartFishingServerEvent",
        "LiquidClippedClientEvent",
        "PlayerAddCustomContainerItemClientEvent",
        "PlayerRemoveCustomContainerItemClientEvent",
        "PhysxTriggerClientEvent",
    }
}

MIGRATION_ALIASES = {
    "PlayerDestoryBlock": ("PlayerDestroyBlock", "拼写错误，请使用 PlayerDestroyBlock。"),
    "EntityUseItemToPos": ("UseItemToPos", "接口已废弃，请使用 UseItemToPos。"),
    "SetShearsDestoryBlockSpeed": (
        "SetShearsDestroyBlockSpeed",
        "拼写错误，请使用 SetShearsDestroyBlockSpeed。",
    ),
    "CancelShearsDestoryBlockSpeed": (
        "CancelShearsDestroyBlockSpeed",
        "拼写错误，请使用 CancelShearsDestroyBlockSpeed。",
    ),
    "CancelShearsDestoryBlockSpeedAll": (
        "CancelShearsDestroyBlockSpeedAll",
        "拼写错误，请使用 CancelShearsDestroyBlockSpeedAll。",
    ),
}

Fetcher = Callable[[str], Tuple[str, Mapping[str, str]]]


@dataclass
class SyncPlan:
    """完成验证、尚未落盘的同步结果。"""

    version: str
    files: Dict[Path, str]
    discovered_doc_paths: List[str]
    api_entries: List[Dict[str, object]]
    event_entries: List[Dict[str, object]]
    target_names: Set[str]
    python_whitelist_modules: List[str]
    python_whitelist_entries: List[Dict[str, str]]


class MarkdownConverter(HTMLParser):
    """足以转换官方参考页的轻量 HTML -> Markdown 转换器。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.list_stack: List[str] = []
        self.in_pre = False
        self.in_code = False
        self.in_table = False
        self.in_cell = False
        self.current_row: List[str] = []
        self.current_cell: List[str] = []
        self.table_header_written = False
        self.skip_anchor_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: Sequence[Tuple[str, Optional[str]]],
    ) -> None:
        attrs_map = dict(attrs)
        css_class = attrs_map.get("class", "") or ""
        if tag == "a" and "header-anchor" in css_class:
            self.skip_anchor_depth += 1
            return
        if self.skip_anchor_depth:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._blank()
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "p":
            self._blank()
        elif tag in ("ul", "ol"):
            self.list_stack.append(tag)
            self._blank()
        elif tag == "li":
            self._blank()
            marker = "1. " if self.list_stack and self.list_stack[-1] == "ol" else "- "
            self.out.append(marker)
        elif tag == "br":
            self.out.append("\n")
        elif tag == "strong":
            self.out.append("**")
        elif tag == "em":
            self.out.append("*")
        elif tag == "pre":
            self.in_pre = True
            language = "python" if "language-python" in css_class else ""
            self._blank()
            self.out.append("```{}\n".format(language))
        elif tag == "code" and not self.in_pre:
            self.in_code = True
            self.out.append("`")
        elif tag == "table":
            self.in_table = True
            self.table_header_written = False
            self._blank()
        elif tag == "tr" and self.in_table:
            self.current_row = []
        elif tag in ("td", "th") and self.in_table:
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if self.skip_anchor_depth:
            if tag == "a":
                self.skip_anchor_depth -= 1
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li"):
            self._blank()
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            self._blank()
        elif tag == "strong":
            self.out.append("**")
        elif tag == "em":
            self.out.append("*")
        elif tag == "pre":
            self.out.append("\n```\n")
            self.in_pre = False
        elif tag == "code" and self.in_code and not self.in_pre:
            self.out.append("`")
            self.in_code = False
        elif tag in ("td", "th") and self.in_table:
            self.current_row.append(normalize_text("".join(self.current_cell)))
            self.in_cell = False
            self.current_cell = []
        elif tag == "tr" and self.in_table and self.current_row:
            row = "| " + " | ".join(cell.replace("|", "\\|") for cell in self.current_row) + " |\n"
            self.out.append(row)
            if not self.table_header_written:
                self.out.append("| " + " | ".join("---" for _ in self.current_row) + " |\n")
                self.table_header_written = True
            self.current_row = []
        elif tag == "table":
            self.in_table = False
            self._blank()

    def handle_data(self, data: str) -> None:
        if self.skip_anchor_depth:
            return
        if self.in_cell:
            self.current_cell.append(data)
        else:
            self.out.append(data)

    def _blank(self) -> None:
        if not self.out:
            return
        text = "".join(self.out)
        if text.endswith("\n\n"):
            return
        self.out.append("\n" if text.endswith("\n") else "\n\n")

    def markdown(self) -> str:
        text = unescape("".join(self.out))
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def normalize_text(value: str) -> str:
    value = unescape(re.sub(r"<[^>]+>", " ", value))
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def official_url(doc_path: str) -> str:
    encoded = "/".join(urllib.parse.quote(part, safe="") for part in doc_path.split("/"))
    return "{}/{}.html".format(BASE_URL, encoded)


def fetch(url: str) -> Tuple[str, Dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=45) as response:
        html = response.read().decode("utf-8", "replace")
        return html, {key: value for key, value in response.headers.items()}


def extract_main_html(html: str) -> str:
    marker = '<div class="theme-default-content content__default">'
    start = html.find(marker)
    if start == -1:
        raise ValueError("找不到官方文档正文容器")
    start += len(marker)
    candidates = [
        index
        for index in (
            html.find('<div class="page-info-hide"', start),
            html.find("<footer", start),
            html.find("</main>", start),
        )
        if index != -1
    ]
    end = min(candidates) if candidates else len(html)
    return html[start:end]


def html_to_markdown(main_html: str) -> str:
    parser = MarkdownConverter()
    parser.feed(main_html)
    parser.close()
    return parser.markdown()


def render_doc(markdown: str, url: str, last_modified: str) -> str:
    return (
        "---\n"
        'source_url: "{}"\n'
        'last_modified: "{}"\n'
        'synced_from: "NetEase developer official website"\n'
        "---\n\n"
        "{}"
    ).format(url, last_modified, markdown)


def extract_official_links(changelog_html: str) -> Dict[str, str]:
    main_html = extract_main_html(changelog_html)
    links: Dict[str, str] = {}
    pattern = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)
    for href, label_html in pattern.findall(main_html):
        label = normalize_text(label_html)
        if label and "/1-ModAPI/" in href:
            links[label] = href
    return links


def href_to_doc_path(href: str) -> str:
    path = urllib.parse.urlparse(href).path
    marker = "/1-ModAPI/"
    if marker not in path:
        raise ValueError("非 ModAPI 官方链接: {}".format(href))
    path = urllib.parse.unquote(path.split(marker, 1)[1])
    if path.endswith(".html"):
        path = path[:-5]
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise ValueError("非法官方文档路径: {}".format(href))
    return "/".join(parts)


def href_to_anchor(href: str, name: str) -> str:
    fragment = urllib.parse.urlparse(href).fragment
    return urllib.parse.unquote(fragment) if fragment else name.lower()


def absolute_source_url(href: str) -> str:
    return urllib.parse.urljoin(SITE_URL, href)


def doc_class_path_from_href(href: str) -> List[str]:
    doc_path = href_to_doc_path(href)
    for prefix in ("接口/", "事件/"):
        if doc_path.startswith(prefix):
            return [doc_path[len(prefix):]]
    return [doc_path]


def parse_index_rows(index_html: str) -> Dict[str, List[Dict[str, str]]]:
    rows: Dict[str, List[Dict[str, str]]] = {}
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", extract_main_html(index_html), re.S | re.I):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)
        if len(cells) < 3:
            continue
        link_match = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', cells[0], re.S | re.I)
        if not link_match:
            continue
        name = normalize_text(link_match.group(2))
        if not re.match(r"^[A-Za-z_]\w*$", name):
            continue
        entry = {
            "name": name,
            "href": link_match.group(1),
            "side": normalize_text(cells[1]),
            "desc": normalize_text(cells[2]),
        }
        rows.setdefault(name, []).append(entry)
    return rows


def section_for_anchor(page_html: str, anchor: str) -> str:
    pattern = re.compile(r'<h2\b[^>]*\bid=["\']{}["\'][^>]*>'.format(re.escape(anchor)), re.I)
    match = pattern.search(page_html)
    if not match:
        return ""
    next_match = re.search(r"<h2\b", page_html[match.end():], re.I)
    end = match.end() + next_match.start() if next_match else len(page_html)
    return page_html[match.start():end]


def split_side_sections(section_html: str) -> Dict[str, str]:
    headings = list(re.finditer(r"<h3\b[^>]*>(.*?)</h3>", section_html, re.S | re.I))
    sections: Dict[str, str] = {}
    for index, heading in enumerate(headings):
        title = normalize_text(heading.group(1))
        side = ""
        if "服务端接口" in title:
            side = "服务端"
        elif "客户端接口" in title:
            side = "客户端"
        if not side:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(section_html)
        sections[side] = section_html[heading.start():end]
    return sections


def strip_span_side(section_html: str) -> List[str]:
    return [
        normalize_text(side)
        for side in re.findall(r"<span\b[^>]*>\s*(服务端|客户端)\s*</span>", section_html, re.S)
    ]


def parse_table(table_html: str) -> List[List[str]]:
    rows: List[List[str]] = []
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_html, re.S | re.I):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)
        if cells:
            rows.append([normalize_text(cell) for cell in cells])
    return rows


def _table_after_label(section_html: str, label: str) -> str:
    match = re.search(
        r">\s*{}\s*</p>\s*<table\b.*?</table>".format(re.escape(label)),
        section_html,
        re.S | re.I,
    )
    if not match:
        return ""
    table_match = re.search(r"<table\b.*?</table>", match.group(0), re.S | re.I)
    return table_match.group(0) if table_match else ""


def parse_params(section_html: str) -> List[Dict[str, str]]:
    table_html = _table_after_label(section_html, "参数")
    rows = parse_table(table_html) if table_html else []
    return [
        {
            "param_comment": row[2],
            "param_name": row[0],
            "param_type": row[1],
        }
        for row in rows[1:]
        if len(row) >= 3
    ]


def parse_return(section_html: str) -> Dict[str, str]:
    table_html = _table_after_label(section_html, "返回值")
    rows = parse_table(table_html) if table_html else []
    if len(rows) > 1 and len(rows[1]) >= 2:
        return {"return_type": rows[1][0], "return_comment": rows[1][1]}
    if re.search(r">\s*返回值\s*</p>\s*<p\b[^>]*>\s*无\s*</p>", section_html, re.S):
        return {"return_type": "", "return_comment": "无"}
    return {"return_type": "", "return_comment": ""}


def parse_desc(section_html: str, fallback: str) -> str:
    match = re.search(r">\s*描述\s*</p>\s*<p\b[^>]*>(.*?)</p>", section_html, re.S | re.I)
    return normalize_text(match.group(1)) if match else fallback


def parse_remarks(section_html: str) -> List[str]:
    marker = re.search(r">\s*备注\s*</p>", section_html, re.S | re.I)
    if not marker:
        return []
    tail = section_html[marker.end():]
    end_markers = [
        match.start()
        for match in (
            re.search(r"<li\b[^>]*>\s*<p\b[^>]*>\s*示例\s*</p>", tail, re.S | re.I),
            re.search(r"<h3\b", tail, re.I),
        )
        if match
    ]
    block = tail[:min(end_markers)] if end_markers else tail
    items = [normalize_text(item) for item in re.findall(r"<li\b[^>]*>(.*?)</li>", block, re.S | re.I)]
    items = [item for item in items if item]
    if not items:
        text = normalize_text(block)
        if text:
            items = [text]
    return list(dict.fromkeys(items))


def _code_text(code_html: str) -> str:
    text = unescape(re.sub(r"<[^>]+>", "", code_html))
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    return "\n".join(line.rstrip() for line in text.strip().split("\n"))


def parse_examples(section_html: str) -> List[str]:
    examples = [
        _code_text(code_html)
        for code_html in re.findall(r"<pre\b[^>]*>\s*<code\b[^>]*>(.*?)</code>\s*</pre>", section_html, re.S | re.I)
    ]
    return [example for example in examples if example]


def choose_method_path(section_html: str, side: str, fallback: str) -> str:
    paths = re.findall(r"method\s+in\s+([A-Za-z0-9_.]+)", section_html)
    if not paths:
        return fallback
    selected = paths[0]
    if side == "服务端":
        for path in paths:
            if ".server." in path or path.endswith("Server"):
                selected = path
                break
    if side == "客户端":
        for path in paths:
            if ".client." in path or path.endswith("Client"):
                selected = path
                break
    # interface.json 的既有类路径从 server/client 起始，不包含 Python 包根 mod。
    return selected[4:] if selected.startswith("mod.") else selected


def _header_value(headers: Mapping[str, str], name: str) -> str:
    name_lower = name.lower()
    for key, value in headers.items():
        if str(key).lower() == name_lower:
            return str(value)
    return ""


def _page_html_for_href(href: str, html_by_doc: Mapping[str, str]) -> str:
    return html_by_doc.get(href_to_doc_path(href), "")


def build_structured_entries(
    target_names: Iterable[str],
    rows_by_name: Mapping[str, List[Dict[str, str]]],
    official_links: Mapping[str, str],
    html_by_doc: Mapping[str, str],
    headers_by_doc: Mapping[str, Mapping[str, str]],
    entry_type: str,
    version: str,
) -> List[Dict[str, object]]:
    output: List[Dict[str, object]] = []
    seen: Set[Tuple[str, str, str]] = set()
    for name in sorted(set(target_names)):
        rows = [dict(row) for row in rows_by_name.get(name, [])]
        if not rows and name in official_links:
            href = official_links[name]
            page_html = _page_html_for_href(href, html_by_doc)
            section_html = section_for_anchor(page_html, href_to_anchor(href, name))
            sides = list(split_side_sections(section_html).keys()) or strip_span_side(section_html) or [""]
            rows = [{"name": name, "href": href, "side": side, "desc": ""} for side in dict.fromkeys(sides)]

        for row in rows:
            href = row["href"]
            doc_path = href_to_doc_path(href)
            page_html = _page_html_for_href(href, html_by_doc)
            whole_section = section_for_anchor(page_html, href_to_anchor(href, name))
            side_sections = split_side_sections(whole_section)
            side = row.get("side", "")
            if not side:
                sides = strip_span_side(whole_section)
                side = sides[0] if sides else ""
            section_html = side_sections.get(side, whole_section)
            identity = (name, side, href)
            if identity in seen:
                continue
            seen.add(identity)

            if entry_type == "event":
                class_path = "server.serverEvent" if side == "服务端" else "client.clientEvent"
            else:
                class_path = choose_method_path(section_html, side, href)

            output.append({
                "name": name,
                "path": class_path,
                "desc": parse_desc(section_html, row.get("desc", "")),
                "doc_class_path": doc_class_path_from_href(href),
                "param": parse_params(section_html),
                "return": parse_return(section_html),
                "state": [{
                    "comment": "来自网易开发者官网 {} 文档".format(version),
                    "operation": "同步",
                    "version": version,
                }],
                "side": side,
                "remarks": parse_remarks(section_html),
                "examples": parse_examples(section_html),
                "source_url": absolute_source_url(href),
                "source_last_modified": _header_value(headers_by_doc.get(doc_path, {}), "Last-Modified"),
            })
    return output


def _load_json_object(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON 根节点必须是对象: {}".format(path))
    return data


def _merge_state(existing: object, current: object, version: str) -> List[object]:
    states = [
        copy.deepcopy(item)
        for item in (existing if isinstance(existing, list) else [])
        if not (isinstance(item, dict) and str(item.get("version", "")) == version)
    ]
    if isinstance(current, list):
        states.extend(copy.deepcopy(current))
    return states


def merge_entry(data: Dict[str, Any], incoming: Dict[str, object], version: str) -> None:
    name = str(incoming.get("name", ""))
    side = str(incoming.get("side", ""))
    existing: Optional[Dict[str, object]] = None

    for entries in data.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == name and str(entry.get("side", "")) == side:
                if existing is None:
                    existing = entry

    merged: Dict[str, object] = copy.deepcopy(existing) if existing else {}
    old_state = merged.get("state", [])
    merged.update(copy.deepcopy(incoming))
    merged["state"] = _merge_state(old_state, incoming.get("state", []), version)

    target_path = str(incoming.get("path", ""))
    empty_keys: List[str] = []
    inserted = False
    for class_path, entries in list(data.items()):
        if not isinstance(entries, list):
            continue
        kept: List[object] = []
        for entry in entries:
            matches = (
                isinstance(entry, dict)
                and entry.get("name") == name
                and str(entry.get("side", "")) == side
            )
            if matches:
                if class_path == target_path and not inserted:
                    kept.append(merged)
                    inserted = True
                continue
            kept.append(entry)
        if kept:
            data[class_path] = kept
        else:
            empty_keys.append(class_path)
    for class_path in empty_keys:
        del data[class_path]

    if not inserted:
        data.setdefault(target_path, []).append(merged)


def update_migration_aliases(interface_data: Dict[str, Any], version: str) -> None:
    by_name: Dict[str, Dict[str, object]] = {}
    for entries in interface_data.values():
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    by_name[str(entry.get("name", ""))] = entry
    for old_name, (new_name, note) in MIGRATION_ALIASES.items():
        existing = by_name.get(old_name)
        replacement = by_name.get(new_name)
        description = "{} 已废弃：{} 旧名称仅保留用于搜索兼容，新开发请使用 {}。".format(
            version,
            note,
            new_name,
        )
        if existing:
            existing["desc"] = description
            existing["state"] = _merge_state(existing.get("state", []), [{
                "version": version,
                "operation": "废弃",
                "comment": note,
            }], version)
        elif replacement:
            alias = copy.deepcopy(replacement)
            alias["name"] = old_name
            alias["desc"] = description
            alias["state"] = [{
                "version": version,
                "operation": "废弃",
                "comment": note,
            }]
            merge_entry(interface_data, alias, version)


def parse_python_whitelist_entries(html: str) -> List[Dict[str, str]]:
    """保留官方展示文本，同时生成用于精确匹配的规范化模块名。"""
    entries: List[Dict[str, str]] = []
    main_html = extract_main_html(html)
    for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", main_html, re.S | re.I):
        raw_display = normalize_text(cell)
        # 官方表格中的 ast 带有“（3.3新增）”说明，说明文字不属于模块名。
        normalized = re.sub(r"[（(][^）)]*新增[^）)]*[）)]$", "", raw_display).strip()
        if normalized and re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", normalized):
            entries.append({
                "raw_display": raw_display,
                "normalized_name": normalized,
            })
    return entries


def parse_python_whitelist(html: str) -> List[str]:
    return [entry["normalized_name"] for entry in parse_python_whitelist_entries(html)]


def _http_date_to_iso(value: str) -> str:
    if not value:
        return "unknown"
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_python_whitelist_snapshot(
    html: str,
    headers: Mapping[str, str],
    entries: Sequence[Mapping[str, str]],
) -> Dict[str, object]:
    modules = [entry["normalized_name"] for entry in entries]
    module_text = "\n".join(modules) + "\n"
    annotations_removed = {
        entry["raw_display"]: entry["normalized_name"]
        for entry in entries
        if entry["raw_display"] != entry["normalized_name"]
    }
    cell_count = len(re.findall(r"<td\b[^>]*>.*?</td>", extract_main_html(html), re.S | re.I))
    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "1.0",
        "source_id": "netease-python-module-whitelist",
        "source_url": PYTHON_WHITELIST_URL,
        "retrieved_at": retrieved_at,
        "last_modified": _http_date_to_iso(_header_value(headers, "Last-Modified")),
        "source_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "modules_sha256": hashlib.sha256(module_text.encode("utf-8")).hexdigest(),
        "module_count": len(modules),
        "normalization": {
            "discarded_cells": cell_count - len(entries),
            "annotations_removed": annotations_removed,
        },
        "entries": [dict(entry) for entry in entries],
        "modules": modules,
    }


def render_python_whitelist(modules: Sequence[str], last_modified: str) -> str:
    lines = [
        "# Python模块白名单",
        "",
        "本页由网易开发者官网白名单表格同步生成，共 {} 个唯一模块。".format(len(modules)),
        "",
        "| 序号 | 模块 |",
        "| --- | --- |",
    ]
    lines.extend("| {} | `{}` |".format(index, module) for index, module in enumerate(modules, 1))
    return render_doc("\n".join(lines) + "\n", PYTHON_WHITELIST_URL, last_modified)


def _find_config_section(node: object, title: str) -> Optional[Dict[str, Any]]:
    if isinstance(node, dict):
        if node.get("title") == title:
            return node
        for value in node.values():
            found = _find_config_section(value, title)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_config_section(value, title)
            if found:
                return found
    return None


def update_config(config: Dict[str, Any], version: str) -> Dict[str, Any]:
    result = copy.deepcopy(config)
    section = _find_config_section(result, "更新信息")
    if section is None:
        section = {"title": "更新信息", "children": []}
        result.setdefault("children", []).append(section)
    children = section.setdefault("children", [])
    if not isinstance(children, list):
        raise ValueError("config.json 的更新信息 children 必须是数组")
    required_versions = [version]
    if version == "3.9":
        required_versions.append("3.8")
    required_paths = ["/mcdocs/1-ModAPI/更新信息/{}".format(item) for item in required_versions]
    section["children"] = required_paths + [item for item in children if item not in required_paths]
    return result


def _json_text(data: object, legacy_comma_spaces: bool = False) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=4) + "\n"
    if legacy_comma_spaces:
        text = re.sub(r",\n", ", \n", text)
    return text


def _config_json_text(data: object, version: str) -> str:
    text = _json_text(data, legacy_comma_spaces=True)
    versions = [version] + (["3.8"] if version == "3.9" else [])
    for item in versions:
        path = "/mcdocs/1-ModAPI/更新信息/{}".format(item)
        text = text.replace(
            "{}, \n".format(json.dumps(path, ensure_ascii=False)),
            "{},\n".format(json.dumps(path, ensure_ascii=False)),
        )
    return text


def _param_names(entry: Mapping[str, object]) -> List[str]:
    params = entry.get("param", [])
    if not isinstance(params, list):
        return []
    return [str(param.get("param_name", "")) for param in params if isinstance(param, dict)]


def _find_entry(
    entries: Sequence[Dict[str, object]],
    name: str,
    side: str = "",
) -> Dict[str, object]:
    matches = [entry for entry in entries if entry.get("name") == name and (not side or entry.get("side") == side)]
    if len(matches) != 1:
        raise ValueError("{} {} 期望 1 条，实际 {} 条".format(name, side, len(matches)))
    return matches[0]


def _validate_common_entries(
    entries: Sequence[Dict[str, object]],
    target_names: Set[str],
    entry_type: str,
) -> None:
    actual_names = {str(entry.get("name", "")) for entry in entries}
    missing = target_names - actual_names
    if missing:
        raise ValueError("{} 缺少结构化条目: {}".format(entry_type, ", ".join(sorted(missing))))
    for entry in entries:
        for field_name in ("remarks", "examples", "state"):
            if not isinstance(entry.get(field_name), list):
                raise ValueError("{} 的 {} 必须是数组".format(entry.get("name"), field_name))
        for field_name in ("source_url", "source_last_modified"):
            if not isinstance(entry.get(field_name), str):
                raise ValueError("{} 的 {} 必须是字符串".format(entry.get("name"), field_name))


def validate_sync_data(
    version: str,
    api_entries: Sequence[Dict[str, object]],
    event_entries: Sequence[Dict[str, object]],
    api_names: Set[str],
    event_names: Set[str],
    enum_names: Set[str],
    markdown_by_doc: Mapping[str, str],
    python_modules: Sequence[str],
    include_python_whitelist: bool,
) -> None:
    _validate_common_entries(api_entries, api_names, "API")
    _validate_common_entries(event_entries, event_names, "事件")

    for enum_name in enum_names:
        doc_path = "枚举值/{}".format(enum_name)
        if doc_path not in markdown_by_doc:
            raise ValueError("缺少枚举文档: {}".format(enum_name))

    if version == "3.9":
        expected_names = API_39_NAMES | EVENT_39_NAMES | ENUM_39_NAMES
        actual_names = api_names | event_names | enum_names
        if actual_names != expected_names:
            raise ValueError(
                "3.9 名称契约不匹配，缺少 {}，多出 {}".format(
                    sorted(expected_names - actual_names),
                    sorted(actual_names - expected_names),
                )
            )
        if len(api_entries) != 11 or len(event_entries) != 6:
            raise ValueError("3.9 条目数量必须为 11 API + 6 event，实际 {} + {}".format(len(api_entries), len(event_entries)))

        expected_params = {
            ("RemovePlayerAnimationFromState", "客户端"): [
                "animationControllerName", "stateName", "animationName",
            ],
            ("GetBlockCollision", "服务端"): ["pos", "dimensionId", "getAll"],
            ("GetBlockCollision", "客户端"): ["pos", "getAll"],
        }
        for key, params in expected_params.items():
            entry = _find_entry(api_entries, key[0], key[1])
            if _param_names(entry) != params:
                raise ValueError("{} {} 参数契约不匹配: {}".format(key[0], key[1], _param_names(entry)))

        sleep_params = ["playerId", "fullName", "auxData", "dimensionid", "x", "y", "z"]
        for name in ("PlayerSleepServerEvent", "PlayerStopSleepServerEvent"):
            if _param_names(_find_entry(event_entries, name, "服务端")) != sleep_params:
                raise ValueError("{} 参数契约不匹配".format(name))

        chest_params = ["playerId", "x", "y", "z", "fullName", "auxData", "dimensionId", "isLargeChest"]
        for name in ("ClientChestOpenEvent", "ClientChestCloseEvent"):
            if _param_names(_find_entry(event_entries, name, "客户端")) != chest_params:
                raise ValueError("{} 参数契约不匹配".format(name))

        enum_markdown = markdown_by_doc.get("枚举值/OriginGUIName", "")
        missing_values = {
            value
            for value in ORIGIN_GUI_39_VALUES
            if not re.search(r"(?m)^\s*{}\s*=".format(re.escape(value)), enum_markdown)
        }
        if missing_values:
            raise ValueError("OriginGUIName 缺少 3.9 枚举值: {}".format(", ".join(sorted(missing_values))))

    if include_python_whitelist:
        unique_modules = set(python_modules)
        if len(python_modules) != 456 or len(unique_modules) != 456:
            raise ValueError("Python 模块白名单必须包含 456 个唯一条目")
        missing_modules = PYTHON_WHITELIST_REQUIRED - unique_modules
        forbidden_modules = PYTHON_WHITELIST_FORBIDDEN & unique_modules
        if missing_modules or forbidden_modules:
            raise ValueError(
                "Python 模块白名单契约不匹配，缺少 {}，禁用项误入 {}".format(
                    sorted(missing_modules),
                    sorted(forbidden_modules),
                )
            )


def build_sync_plan(
    repo_root: Path,
    version: str = "3.9",
    include_python_whitelist: bool = False,
    delay: float = 0.0,
    fetcher: Fetcher = fetch,
) -> SyncPlan:
    """抓取并验证全部内容；该函数不会创建或修改文件。"""
    if not re.match(r"^\d+\.\d+$", version):
        raise ValueError("非法版本号: {}".format(version))
    repo_root = Path(repo_root).resolve()
    changelog_path = "更新信息/{}".format(version)
    html_by_doc: Dict[str, str] = {}
    headers_by_doc: Dict[str, Mapping[str, str]] = {}

    def fetch_doc(doc_path: str) -> None:
        if doc_path in html_by_doc:
            return
        html, headers = fetcher(official_url(doc_path))
        if not isinstance(html, str):
            raise TypeError("fetcher 必须返回 str HTML")
        html_by_doc[doc_path] = html
        headers_by_doc[doc_path] = dict(headers)
        if delay > 0:
            time.sleep(delay)

    # 更新页和两张索引是发现其他页面所需的唯一固定入口。
    for doc_path in (changelog_path, API_INDEX_PATH, EVENT_INDEX_PATH):
        fetch_doc(doc_path)

    official_links = extract_official_links(html_by_doc[changelog_path])
    api_index_rows = parse_index_rows(html_by_doc[API_INDEX_PATH])
    event_index_rows = parse_index_rows(html_by_doc[EVENT_INDEX_PATH])

    api_names: Set[str] = set(EXTRA_API_NAMES_BY_VERSION.get(version, set()))
    event_names: Set[str] = set(EXTRA_EVENT_NAMES_BY_VERSION.get(version, set()))
    enum_names: Set[str] = set()
    related_doc_paths: Set[str] = {changelog_path, API_INDEX_PATH, EVENT_INDEX_PATH}

    for name, href in official_links.items():
        if not re.match(r"^[A-Za-z_]\w*$", name):
            continue
        doc_path = href_to_doc_path(href)
        if doc_path.startswith("接口/"):
            api_names.add(name)
        elif doc_path.startswith("事件/"):
            event_names.add(name)
        elif doc_path.startswith("枚举值/"):
            enum_names.add(name)
        else:
            continue
        related_doc_paths.add(doc_path)

    for name in api_names:
        for row in api_index_rows.get(name, []):
            related_doc_paths.add(href_to_doc_path(row["href"]))
    for name in event_names:
        for row in event_index_rows.get(name, []):
            related_doc_paths.add(href_to_doc_path(row["href"]))

    for doc_path in sorted(related_doc_paths):
        fetch_doc(doc_path)

    markdown_by_doc: Dict[str, str] = {}
    files: Dict[Path, str] = {}
    for doc_path in sorted(related_doc_paths):
        markdown = html_to_markdown(extract_main_html(html_by_doc[doc_path]))
        markdown_by_doc[doc_path] = markdown
        files[repo_root / "docs" / (doc_path + ".md")] = render_doc(
            markdown,
            official_url(doc_path),
            _header_value(headers_by_doc[doc_path], "Last-Modified"),
        )

    api_entries = build_structured_entries(
        api_names,
        api_index_rows,
        official_links,
        html_by_doc,
        headers_by_doc,
        "api",
        version,
    )
    event_entries = build_structured_entries(
        event_names,
        event_index_rows,
        official_links,
        html_by_doc,
        headers_by_doc,
        "event",
        version,
    )

    interface_path = repo_root / "docs" / "interface.json"
    events_path = repo_root / "docs" / "events.json"
    config_path = repo_root / "docs" / "config.json"
    interface_data = _load_json_object(interface_path)
    events_data = _load_json_object(events_path)
    config_data = _load_json_object(config_path)

    for entry in api_entries:
        merge_entry(interface_data, entry, version)
    if version == "3.8":
        update_migration_aliases(interface_data, version)
    for entry in event_entries:
        merge_entry(events_data, entry, version)

    files[interface_path] = _json_text(interface_data)
    files[events_path] = _json_text(events_data)
    files[config_path] = _config_json_text(update_config(config_data, version), version)

    python_modules: List[str] = []
    python_whitelist_entries: List[Dict[str, str]] = []
    if include_python_whitelist:
        whitelist_html, whitelist_headers = fetcher(PYTHON_WHITELIST_URL)
        python_whitelist_entries = parse_python_whitelist_entries(whitelist_html)
        python_modules = [entry["normalized_name"] for entry in python_whitelist_entries]
        files[repo_root / "docs" / (PYTHON_WHITELIST_DOC_PATH + ".md")] = render_python_whitelist(
            python_modules,
            _header_value(whitelist_headers, "Last-Modified"),
        )
        snapshot_path = (
            repo_root
            / "standard"
            / "registry"
            / "snapshots"
            / "python-module-whitelist.json"
        )
        files[snapshot_path] = _json_text(build_python_whitelist_snapshot(
            whitelist_html,
            whitelist_headers,
            python_whitelist_entries,
        ))

    validate_sync_data(
        version,
        api_entries,
        event_entries,
        api_names,
        event_names,
        enum_names,
        markdown_by_doc,
        python_modules,
        include_python_whitelist,
    )
    # 提前验证所有 JSON 文本可被重新解析，避免提交阶段才暴露序列化问题。
    json.loads(files[interface_path])
    json.loads(files[events_path])
    json.loads(files[config_path])

    return SyncPlan(
        version=version,
        files=files,
        discovered_doc_paths=sorted(related_doc_paths),
        api_entries=api_entries,
        event_entries=event_entries,
        target_names=api_names | event_names | enum_names,
        python_whitelist_modules=python_modules,
        python_whitelist_entries=python_whitelist_entries,
    )


def _read_text_preserving_newlines(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_temp_file(target: Path, content: str) -> Path:
    descriptor, temp_name = tempfile.mkstemp(prefix=".modsdk-sync-", suffix=".tmp", dir=str(target.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
    return temp_path


def write_files_atomically(repo_root: Path, files: Mapping[Path, str]) -> None:
    """以事务方式替换文本文件，任一替换失败时恢复此前目标。"""
    repo_root = Path(repo_root).resolve()
    targets = sorted((Path(path).resolve(), content) for path, content in files.items())
    for target, _ in targets:
        try:
            target.relative_to(repo_root)
        except ValueError as exc:
            raise ValueError("拒绝写入仓库外路径: {}".format(target)) from exc

    created_dirs: Set[Path] = set()
    originals: Dict[Path, Tuple[bool, str]] = {}
    temp_paths: Dict[Path, Path] = {}
    replaced: List[Path] = []
    try:
        for target, content in targets:
            missing: List[Path] = []
            current = target.parent
            while not current.exists() and current != repo_root.parent:
                missing.append(current)
                current = current.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.update(missing)
            originals[target] = (target.exists(), _read_text_preserving_newlines(target) if target.exists() else "")
            temp_paths[target] = _write_temp_file(target, content)

        for target, _ in targets:
            os.replace(str(temp_paths[target]), str(target))
            replaced.append(target)
            del temp_paths[target]
    except Exception as exc:
        rollback_errors: List[str] = []
        for target in reversed(replaced):
            existed, original = originals[target]
            try:
                if existed:
                    rollback_temp = _write_temp_file(target, original)
                    os.replace(str(rollback_temp), str(target))
                elif target.exists():
                    target.unlink()
            except Exception as rollback_exc:
                rollback_errors.append("{}: {}".format(target, rollback_exc))
        for temp_path in temp_paths.values():
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
        for directory in sorted(created_dirs, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        if rollback_errors:
            raise RuntimeError("同步失败且回滚不完整: {}".format("; ".join(rollback_errors))) from exc
        raise


def sync(
    repo_root: Path,
    version: str = "3.9",
    dry_run: bool = False,
    delay: float = 0.2,
    include_python_whitelist: bool = False,
    fetcher: Fetcher = fetch,
) -> SyncPlan:
    plan = build_sync_plan(
        repo_root=Path(repo_root),
        version=version,
        include_python_whitelist=include_python_whitelist,
        delay=delay,
        fetcher=fetcher,
    )
    if not dry_run:
        write_files_atomically(Path(repo_root), plan.files)
    action = "validated" if dry_run else "synced"
    print(
        "{} ModSDK {}: {} docs, {} API entries, {} event entries".format(
            action,
            version,
            len(plan.discovered_doc_paths),
            len(plan.api_entries),
            len(plan.event_entries),
        )
    )
    return plan


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="同步网易 ModSDK 官方文档")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--version", default="3.9")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--include-python-whitelist", action="store_true")
    args = parser.parse_args(argv)

    try:
        sync(
            repo_root=Path(args.repo_root).resolve(),
            version=args.version,
            dry_run=args.dry_run,
            delay=args.delay,
            include_python_whitelist=args.include_python_whitelist,
        )
    except Exception as exc:
        print("official docs sync failed: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
