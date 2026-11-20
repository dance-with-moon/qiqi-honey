# -*- coding: utf-8 -*-
"""聊天记录存储层：优先存 Supabase 免费 Postgres，没配置就退回本地文件。

两种模式，自动选择：
    1. 配了 SUPABASE_URL + SUPABASE_KEY → 存云端，重启/改代码/休眠都不丢
    2. 没配 → 存本地 sessions/ 目录（免费平台重启会丢，但不能让功能坏掉）

建表 SQL 和配置步骤见《部署指南.md》第 10 节。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

TIMEOUT = 10                 # 单次请求超时（秒）
TABLE_DEFAULT = "chat_sessions"


class StorageError(RuntimeError):
    """存储层出错。调用方接住它、给个提示就行，别让整个页面崩掉。"""


class SessionStore:
    def __init__(self, local_dir, url=None, key=None, table=None):
        self.local_dir = Path(local_dir)
        self.url = (url or "").strip().rstrip("/")
        self.key = (key or "").strip()
        self.table = (table or TABLE_DEFAULT).strip()
        self.online = bool(self.url and self.key)

        if not self.online:                  # 本地兜底目录，建不出来也不报错
            try:
                self.local_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    @property
    def mode(self) -> str:
        return "supabase" if self.online else "local"

    # ------------------------------------------------------------------ 对外接口
    def list_sessions(self):
        """会话名列表，时间倒序。名字本身就是时间戳，按字符串排就行。"""
        if not self.online:
            try:
                names = [p.stem for p in self.local_dir.glob("*.json")]
            except OSError:
                names = []
            return sorted(names, reverse=True)

        rows = self._request("GET", params={"select": "id", "order": "id.desc"})
        return [row["id"] for row in rows]

    def load(self, name):
        if not self.online:
            path = self.local_dir / f"{name}.json"
            if not path.exists():
                return None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError) as exc:
                raise StorageError(f"读取本地会话失败：{exc}") from exc

        rows = self._request("GET", params={"id": f"eq.{name}", "select": "*"})
        if not rows:
            return None
        row = rows[0]
        return {
            "messages": row.get("messages") or [],
            "nick_name": row.get("nick_name"),
            "nature": row.get("nature"),
        }

    def save(self, name, data):
        if not self.online:
            path = self.local_dir / f"{name}.json"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except OSError as exc:
                raise StorageError(f"写入本地会话失败：{exc}") from exc
            return

        row = {
            "id": name,
            "messages": data.get("messages", []),
            "nick_name": data.get("nick_name"),
            "nature": data.get("nature"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # 主键冲突就覆盖（upsert），所以不用先查再决定 insert/update
        self._request(
            "POST",
            json_body=[row],
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def delete(self, name):
        if not self.online:
            try:
                (self.local_dir / f"{name}.json").unlink(missing_ok=True)
            except OSError as exc:
                raise StorageError(f"删除本地会话失败：{exc}") from exc
            return

        self._request("DELETE", params={"id": f"eq.{name}"})

    # ------------------------------------------------------------------ 内部
    def _request(self, method, params=None, json_body=None, prefer=None):
        """走 Supabase 的 REST（PostgREST）接口，不依赖数据库驱动。"""
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer

        try:
            resp = requests.request(
                method,
                f"{self.url}/rest/v1/{self.table}",
                headers=headers,
                params=params,
                json=json_body,
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise StorageError(f"连不上云端数据库：{exc}") from exc

        if resp.status_code >= 400:
            raise StorageError(
                f"数据库返回 {resp.status_code}：{resp.text[:200]}（检查 SUPABASE_URL / SUPABASE_KEY 和表名）"
            )

        if method == "GET":
            try:
                return resp.json()
            except ValueError as exc:
                raise StorageError("数据库返回的内容看不懂，可能是 URL 填错了") from exc
        return None
