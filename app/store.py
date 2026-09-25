"""标定谱系存储层。

核心不变量（均在单个 SQLite 写事务内保证）：

1. 一条失效裁决使目标记录及全部可达下游记录在同一持久化提交中失效；
2. 操作标识幂等：重复裁决/发布返回首次结果；同一操作标识改换目标 -> 冲突且不改状态；
3. 新建推导记录时，任一前序不存在 / 已失效 / 自引用 / 成环 -> 整笔拒绝，既有结论不变；
4. 写事务串行化（BEGIN IMMEDIATE），因此“新推导”与“失效裁决”竞争后，
   不可能存在有效记录依赖失效记录；
5. 分支试建：分支创建时保存当时可引用有效记录（含其直接依据）的稳定快照，
   分支内草案条目（D 开头草案编号）不进入正式谱系；
6. 整组发布：发布事务内重新核对全部外部依据仍有效且直接依据未在分支创建后
   发生变化，条件满足才按分支顺序分配正式编号并建立全部引用；任一依据失效或
   谱系已变化 -> 整次发布回滚并返回可定位冲突，主谱系不产生部分记录。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('raw', 'derived')),
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('valid', 'invalid')),
    invalidated_by  TEXT,
    invalidated_at  TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    child_id  TEXT NOT NULL REFERENCES records(id),
    parent_id TEXT NOT NULL REFERENCES records(id),
    seq       INTEGER NOT NULL,
    PRIMARY KEY (child_id, parent_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_parent ON edges(parent_id);
CREATE TABLE IF NOT EXISTS operations (
    operation_id     TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    target_record_id TEXT NOT NULL,
    response_json    TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL CHECK (status IN ('open', 'published')),
    created_at    TEXT NOT NULL,
    published_at  TEXT,
    published_op  TEXT
);
-- 分支创建时刻的稳定快照：当时全部有效记录及其直接依据。
-- 发布时据此核对“外部依据仍有效且直接依据未发生变化”。
CREATE TABLE IF NOT EXISTS branch_snapshot (
    branch_id  TEXT NOT NULL REFERENCES branches(id),
    record_id  TEXT NOT NULL REFERENCES records(id),
    parent_ids TEXT NOT NULL,
    PRIMARY KEY (branch_id, record_id)
);
-- 分支内草案条目：D 开头草案编号，发布前不进入正式谱系。
CREATE TABLE IF NOT EXISTS branch_entries (
    id                  TEXT PRIMARY KEY,
    branch_id           TEXT NOT NULL REFERENCES branches(id),
    seq                 INTEGER NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('raw', 'derived')),
    payload             TEXT NOT NULL,
    published_record_id TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (branch_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_entries_branch ON branch_entries(branch_id, seq);
-- 草案条目的直接依据：parent_kind='record' 引用快照中的正式记录，
-- parent_kind='entry' 引用本分支先前草案条目。
CREATE TABLE IF NOT EXISTS branch_entry_edges (
    child_entry_id TEXT NOT NULL REFERENCES branch_entries(id),
    parent_kind    TEXT NOT NULL CHECK (parent_kind IN ('record', 'entry')),
    parent_id      TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    PRIMARY KEY (child_entry_id, parent_kind, parent_id)
);
"""


class StoreError(Exception):
    """业务校验错误，携带可定位信息。"""

    def __init__(self, code: str, message: str, status: int = 422,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def to_response(self) -> tuple[dict[str, Any], int]:
        return {"error": {"code": self.code, "message": self.message,
                          "details": self.details}}, self.status


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class CalibrationStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # check_same_thread=False + 进程内互斥锁：所有写事务串行，
        # 读也走同一连接，保证读到已提交状态。
        self._conn = sqlite3.connect(db_path, check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get_record(self, record_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)
            ).fetchone()
            if row is None:
                raise StoreError("RECORD_NOT_FOUND",
                                 f"记录 {record_id} 不存在", status=404,
                                 details={"record_id": record_id})
            parents = [r["parent_id"] for r in self._conn.execute(
                "SELECT parent_id FROM edges WHERE child_id=? ORDER BY seq",
                (record_id,))]
            return self._row_to_dict(row, parents)

    def list_records(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM records ORDER BY created_at, id").fetchall()
            edge_rows = self._conn.execute(
                "SELECT child_id, parent_id FROM edges ORDER BY child_id, seq"
            ).fetchall()
        parents: dict[str, list[str]] = {}
        for e in edge_rows:
            parents.setdefault(e["child_id"], []).append(e["parent_id"])
        return [self._row_to_dict(r, parents.get(r["id"], [])) for r in rows]

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT response_json FROM operations WHERE operation_id=?",
                (operation_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row["response_json"])

    # ------------------------------------------------------------------ #
    # 分支读取
    # ------------------------------------------------------------------ #
    def list_branches(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM branches ORDER BY created_at, id").fetchall()
            result = []
            for r in rows:
                snap_size = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM branch_snapshot WHERE branch_id=?",
                    (r["id"],)).fetchone()["n"]
                entry_count = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM branch_entries WHERE branch_id=?",
                    (r["id"],)).fetchone()["n"]
                result.append({
                    "id": r["id"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "published_at": r["published_at"],
                    "published_op": r["published_op"],
                    "snapshot_size": snap_size,
                    "entry_count": entry_count,
                })
            return result

    def get_branch(self, branch_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM branches WHERE id=?", (branch_id,)).fetchone()
            if row is None:
                raise StoreError("BRANCH_NOT_FOUND",
                                 f"分支 {branch_id} 不存在", status=404,
                                 details={"branch_id": branch_id})
            snapshot_ids = [r["record_id"] for r in self._conn.execute(
                "SELECT record_id FROM branch_snapshot WHERE branch_id=? "
                "ORDER BY record_id", (branch_id,))]
            entry_rows = self._conn.execute(
                "SELECT * FROM branch_entries WHERE branch_id=? "
                "ORDER BY seq", (branch_id,)).fetchall()
            edge_rows = self._conn.execute(
                "SELECT child_entry_id, parent_kind, parent_id "
                "FROM branch_entry_edges WHERE child_entry_id IN "
                "(SELECT id FROM branch_entries WHERE branch_id=?) "
                "ORDER BY child_entry_id, seq", (branch_id,)).fetchall()
        parents: dict[str, list[dict[str, str]]] = {}
        for e in edge_rows:
            parents.setdefault(e["child_entry_id"], []).append(
                {"kind": e["parent_kind"], "id": e["parent_id"]})
        entries = [{
            "id": er["id"],
            "seq": er["seq"],
            "kind": er["kind"],
            "payload": json.loads(er["payload"]),
            "parents": parents.get(er["id"], []),
            "published_record_id": er["published_record_id"],
            "created_at": er["created_at"],
        } for er in entry_rows]
        return {
            "id": row["id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            "published_op": row["published_op"],
            "snapshot_record_ids": snapshot_ids,
            "entries": entries,
        }

    @staticmethod
    def _row_to_dict(row: sqlite3.Row, parents: list[str]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "parent_ids": parents,
            "invalidated_by": row["invalidated_by"],
            "invalidated_at": row["invalidated_at"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------------ #
    # 创建原始 / 推导记录
    # ------------------------------------------------------------------ #
    def create_record(self, kind: str, payload: dict[str, Any],
                      parent_ids: list[str] | None,
                      record_id: str | None = None) -> dict[str, Any]:
        if kind not in ("raw", "derived"):
            raise StoreError("INVALID_KIND", f"未知记录类型 {kind!r}",
                             status=400, details={"kind": kind})
        parent_ids = list(parent_ids or [])

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rid = record_id or self._allocate_id_locked()

                if self._conn.execute(
                        "SELECT 1 FROM records WHERE id=?", (rid,)).fetchone():
                    raise StoreError("RECORD_ID_CONFLICT",
                                     f"记录编号 {rid} 已存在", status=409,
                                     details={"record_id": rid})

                if kind == "raw" and parent_ids:
                    raise StoreError(
                        "RAW_RECORD_HAS_PARENTS",
                        "原始标定记录不能引用前序记录", status=400,
                        details={"parent_ids": parent_ids})
                if kind == "derived" and not parent_ids:
                    raise StoreError(
                        "DERIVED_RECORD_WITHOUT_BASIS",
                        "推导标定记录必须至少选择一个当前有效的前序记录",
                        details={"record_id": rid})

                # 自引用（id 尚未落库也必须拦截）
                if rid in parent_ids:
                    raise StoreError(
                        "SELF_REFERENCE",
                        f"推导记录 {rid} 不能引用自身作为前序依据",
                        details={"record_id": rid, "parent_ids": parent_ids})

                if len(set(parent_ids)) != len(parent_ids):
                    dup = sorted({p for p in parent_ids
                                  if parent_ids.count(p) > 1})
                    raise StoreError("DUPLICATE_PARENT",
                                     "前序记录重复出现",
                                     details={"duplicate_parent_ids": dup})

                # 存在性 + 有效性校验（全部在写事务内读到的是已提交快照）
                placeholders = ",".join("?" * len(parent_ids))
                found = {r["id"]: r for r in self._conn.execute(
                    f"SELECT id, status FROM records WHERE id IN ({placeholders})",
                    parent_ids)} if parent_ids else {}
                missing = [p for p in parent_ids if p not in found]
                if missing:
                    raise StoreError(
                        "PARENT_NOT_FOUND",
                        f"前序记录 {', '.join(missing)} 不存在",
                        details={"missing_parent_ids": missing})
                invalid_parents = [p for p in parent_ids
                                   if found[p]["status"] != "valid"]
                if invalid_parents:
                    raise StoreError(
                        "PARENT_INVALID",
                        f"前序记录 {', '.join(invalid_parents)} 已失效，"
                        "不能作为新推导的依据",
                        details={"invalid_parent_ids": invalid_parents})

                # 成环检查：新节点沿 parent 方向可达自身即成环。
                if self._reaches_ancestor_locked(rid, set(parent_ids)):
                    raise StoreError(
                        "CYCLE_DETECTED",
                        "引用关系形成环",
                        details={"record_id": rid, "parent_ids": parent_ids})

                now = _utcnow()
                self._conn.execute(
                    "INSERT INTO records (id, kind, payload, status, "
                    "invalidated_by, invalidated_at, created_at) "
                    "VALUES (?, ?, ?, 'valid', NULL, NULL, ?)",
                    (rid, kind, json.dumps(payload, ensure_ascii=False), now))
                for seq, pid in enumerate(parent_ids):
                    self._conn.execute(
                        "INSERT INTO edges (child_id, parent_id, seq) "
                        "VALUES (?, ?, ?)", (rid, pid, seq))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        return self.get_record(rid)

    def _allocate_id_locked(self) -> str:
        return self._allocate_seq_locked("seq", "R")

    def _reaches_ancestor_locked(self, target: str,
                                 starts: set[str]) -> bool:
        """从 starts 沿 child->parent 边向上能否到达 target。"""
        if not starts:
            return False
        frontier = set(starts)
        seen: set[str] = set()
        while frontier:
            if target in frontier:
                return True
            seen |= frontier
            qmarks = ",".join("?" * len(frontier))
            rows = self._conn.execute(
                f"SELECT parent_id FROM edges WHERE child_id IN ({qmarks})",
                tuple(frontier)).fetchall()
            frontier = {r["parent_id"] for r in rows} - seen
        return False

    # ------------------------------------------------------------------ #
    # 失效裁决（级联，单事务）
    # ------------------------------------------------------------------ #
    def invalidate(self, operation_id: str,
                   target_id: str) -> dict[str, Any]:
        if not operation_id:
            raise StoreError("OPERATION_ID_REQUIRED",
                             "失效裁决必须携带操作标识", status=400)

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # 1) 操作标识幂等 / 冲突判定（在同一写事务内）
                op = self._conn.execute(
                    "SELECT kind, target_record_id, response_json "
                    "FROM operations WHERE operation_id=?",
                    (operation_id,)).fetchone()
                if op is not None:
                    if (op["kind"] == "invalidate"
                            and op["target_record_id"] == target_id):
                        # 重复同一裁决：原样返回首次结果，不改状态
                        response = json.loads(op["response_json"])
                        response["replayed"] = True
                        self._conn.execute("COMMIT")
                        return response
                    raise StoreError(
                        "OPERATION_CONFLICT",
                        f"操作标识 {operation_id} 已用于 "
                        f"{op['kind']}({op['target_record_id']})，"
                        f"不能改用于 invalidate({target_id})",
                        status=409,
                        details={"operation_id": operation_id,
                                 "original_target": op["target_record_id"],
                                 "conflicting_target": target_id})

                # 2) 目标必须存在（不记录该操作标识，允许客户端修正后重试）
                target = self._conn.execute(
                    "SELECT id, status FROM records WHERE id=?", (target_id,)
                ).fetchone()
                if target is None:
                    raise StoreError(
                        "RECORD_NOT_FOUND",
                        f"裁决目标记录 {target_id} 不存在", status=404,
                        details={"record_id": target_id,
                                 "operation_id": operation_id})
                if target["status"] != "valid":
                    # 已有稳定失效来源，不得被新裁决覆盖
                    raise StoreError(
                        "RECORD_ALREADY_INVALID",
                        f"记录 {target_id} 已失效，失效来源稳定，"
                        "不能再次裁决",
                        status=409,
                        details={"record_id": target_id,
                                 "operation_id": operation_id})

                # 3) 求目标 + 全部可达下游闭包
                closure = self._downstream_closure_locked(target_id)

                # 4) 同一提交内将闭包中仍有效的节点失效；
                #    早已失效的节点保留其首次失效来源（稳定来源）。
                now = _utcnow()
                self._conn.execute(
                    "UPDATE records SET status='invalid', "
                    "invalidated_by=?, invalidated_at=? "
                    "WHERE id IN (%s) AND status='valid'"
                    % ",".join("?" * len(closure)),
                    (target_id, now, *closure))

                response = {
                    "operation_id": operation_id,
                    "result": "completed",
                    "replayed": False,
                    "target_record_id": target_id,
                    "cascade": [
                        {"id": rid, "invalidated_by": target_id}
                        for rid in closure
                    ],
                }
                self._conn.execute(
                    "INSERT INTO operations (operation_id, kind, "
                    "target_record_id, response_json, created_at) "
                    "VALUES (?, 'invalidate', ?, ?, ?)",
                    (operation_id, target_id,
                     json.dumps(response, ensure_ascii=False), now))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        return response

    def _downstream_closure_locked(self, target_id: str) -> list[str]:
        """目标及其全部可达下游（沿 parent->child 传播）。"""
        rows = self._conn.execute(
            """
            WITH RECURSIVE reach(id) AS (
                SELECT ?
                UNION
                SELECT e.child_id
                FROM edges e JOIN reach r ON e.parent_id = r.id
            )
            SELECT id FROM reach ORDER BY id
            """, (target_id,)).fetchall()
        return [r["id"] for r in rows]

    # ------------------------------------------------------------------ #
    # 分支：创建（含稳定快照）
    # ------------------------------------------------------------------ #
    def create_branch(self) -> dict[str, Any]:
        """创建试建分支：保存当时全部有效记录及其直接依据的稳定快照。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                bid = self._allocate_seq_locked("branch_seq", "B")
                now = _utcnow()
                self._conn.execute(
                    "INSERT INTO branches (id, status, created_at, "
                    "published_at, published_op) VALUES (?, 'open', ?, NULL, NULL)",
                    (bid, now))
                valid_rows = self._conn.execute(
                    "SELECT id FROM records WHERE status='valid' ORDER BY id"
                ).fetchall()
                for vr in valid_rows:
                    parents = [e["parent_id"] for e in self._conn.execute(
                        "SELECT parent_id FROM edges WHERE child_id=? "
                        "ORDER BY seq", (vr["id"],))]
                    self._conn.execute(
                        "INSERT INTO branch_snapshot "
                        "(branch_id, record_id, parent_ids) VALUES (?, ?, ?)",
                        (bid, vr["id"], json.dumps(parents)))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_branch(bid)

    def _allocate_seq_locked(self, key: str, prefix: str) -> str:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        seq = (int(row["value"]) + 1) if row else 1
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(seq)))
        return f"{prefix}{seq:06d}"

    # ------------------------------------------------------------------ #
    # 分支：草案条目（不进入正式谱系）
    # ------------------------------------------------------------------ #
    def create_entry(self, branch_id: str, kind: str, payload: dict[str, Any],
                     parent_refs: list[str] | None,
                     entry_id: str | None = None) -> dict[str, Any]:
        if kind not in ("raw", "derived"):
            raise StoreError("INVALID_KIND", f"未知记录类型 {kind!r}",
                             status=400, details={"kind": kind})
        parent_refs = list(parent_refs or [])

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                branch = self._conn.execute(
                    "SELECT id, status FROM branches WHERE id=?",
                    (branch_id,)).fetchone()
                if branch is None:
                    raise StoreError("BRANCH_NOT_FOUND",
                                     f"分支 {branch_id} 不存在", status=404,
                                     details={"branch_id": branch_id})
                if branch["status"] != "open":
                    raise StoreError(
                        "BRANCH_CLOSED",
                        f"分支 {branch_id} 已发布，不能再追加草案条目",
                        status=409, details={"branch_id": branch_id})

                eid = entry_id or self._allocate_seq_locked("entry_seq", "D")
                if self._conn.execute(
                        "SELECT 1 FROM branch_entries WHERE id=?",
                        (eid,)).fetchone():
                    raise StoreError("ENTRY_ID_CONFLICT",
                                     f"草案编号 {eid} 已存在", status=409,
                                     details={"entry_id": eid})

                if kind == "raw" and parent_refs:
                    raise StoreError(
                        "RAW_RECORD_HAS_PARENTS",
                        "原始标定记录不能引用前序记录", status=400,
                        details={"parent_ids": parent_refs})
                if kind == "derived" and not parent_refs:
                    raise StoreError(
                        "DERIVED_RECORD_WITHOUT_BASIS",
                        "推导标定记录必须至少选择一个前序记录"
                        "（快照中的有效记录或本分支先前条目）",
                        details={"entry_id": eid})
                if eid in parent_refs:
                    raise StoreError(
                        "SELF_REFERENCE",
                        f"草案条目 {eid} 不能引用自身作为前序依据",
                        details={"entry_id": eid, "parent_ids": parent_refs})
                if len(set(parent_refs)) != len(parent_refs):
                    dup = sorted({p for p in parent_refs
                                  if parent_refs.count(p) > 1})
                    raise StoreError("DUPLICATE_PARENT", "前序记录重复出现",
                                     details={"duplicate_parent_ids": dup})

                # 逐条解析引用：快照中的正式记录 或 本分支先前草案条目
                snapshot = {r["record_id"] for r in self._conn.execute(
                    "SELECT record_id FROM branch_snapshot WHERE branch_id=?",
                    (branch_id,))}
                own_entries = {r["id"] for r in self._conn.execute(
                    "SELECT id FROM branch_entries WHERE branch_id=?",
                    (branch_id,))}
                missing: list[str] = []
                not_in_snapshot: list[str] = []
                foreign_entries: list[str] = []
                resolved: list[tuple[str, str]] = []  # (parent_kind, parent_id)
                for ref in parent_refs:
                    if ref in snapshot:
                        resolved.append(("record", ref))
                    elif ref in own_entries:
                        resolved.append(("entry", ref))
                    else:
                        is_record = self._conn.execute(
                            "SELECT 1 FROM records WHERE id=?",
                            (ref,)).fetchone()
                        is_entry = self._conn.execute(
                            "SELECT branch_id FROM branch_entries WHERE id=?",
                            (ref,)).fetchone()
                        if is_entry is not None:
                            foreign_entries.append(ref)
                        elif is_record is not None:
                            not_in_snapshot.append(ref)
                        else:
                            missing.append(ref)
                if missing:
                    raise StoreError(
                        "PARENT_NOT_FOUND",
                        f"前序记录 {', '.join(missing)} 不存在",
                        details={"missing_parent_ids": missing,
                                 "branch_id": branch_id})
                if foreign_entries:
                    raise StoreError(
                        "ENTRY_NOT_IN_BRANCH",
                        f"草案条目 {', '.join(foreign_entries)} 不属于分支 "
                        f"{branch_id}，不能跨分支引用",
                        details={"foreign_entry_ids": foreign_entries,
                                 "branch_id": branch_id})
                if not_in_snapshot:
                    raise StoreError(
                        "BASIS_NOT_IN_SNAPSHOT",
                        f"记录 {', '.join(not_in_snapshot)} 不在分支 "
                        f"{branch_id} 创建时的有效快照中，不能作为依据",
                        details={"record_ids": not_in_snapshot,
                                 "branch_id": branch_id})

                seq_row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS n "
                    "FROM branch_entries WHERE branch_id=?",
                    (branch_id,)).fetchone()
                now = _utcnow()
                self._conn.execute(
                    "INSERT INTO branch_entries (id, branch_id, seq, kind, "
                    "payload, published_record_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    (eid, branch_id, seq_row["n"], kind,
                     json.dumps(payload, ensure_ascii=False), now))
                for seq, (pkind, pid) in enumerate(resolved):
                    self._conn.execute(
                        "INSERT INTO branch_entry_edges (child_entry_id, "
                        "parent_kind, parent_id, seq) VALUES (?, ?, ?, ?)",
                        (eid, pkind, pid, seq))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        return self._get_entry(branch_id, eid)

    def _get_entry(self, branch_id: str, entry_id: str) -> dict[str, Any]:
        branch = self.get_branch(branch_id)
        for entry in branch["entries"]:
            if entry["id"] == entry_id:
                return entry
        raise StoreError("ENTRY_NOT_FOUND",
                         f"草案条目 {entry_id} 不存在于分支 {branch_id}",
                         status=404,
                         details={"entry_id": entry_id,
                                  "branch_id": branch_id})

    # ------------------------------------------------------------------ #
    # 分支：整组发布（单事务核对 + 分配正式编号）
    # ------------------------------------------------------------------ #
    def publish_branch(self, operation_id: str,
                       branch_id: str) -> dict[str, Any]:
        if not operation_id:
            raise StoreError("OPERATION_ID_REQUIRED",
                             "发布必须携带操作标识", status=400)

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # 1) 操作标识幂等 / 冲突判定（与失效裁决共用同一标识空间）
                op = self._conn.execute(
                    "SELECT kind, target_record_id, response_json "
                    "FROM operations WHERE operation_id=?",
                    (operation_id,)).fetchone()
                if op is not None:
                    if (op["kind"] == "publish"
                            and op["target_record_id"] == branch_id):
                        response = json.loads(op["response_json"])
                        response["replayed"] = True
                        self._conn.execute("COMMIT")
                        return response
                    raise StoreError(
                        "OPERATION_CONFLICT",
                        f"操作标识 {operation_id} 已用于 "
                        f"{op['kind']}({op['target_record_id']})，"
                        f"不能改用于 publish({branch_id})",
                        status=409,
                        details={"operation_id": operation_id,
                                 "original_target": op["target_record_id"],
                                 "conflicting_target": branch_id})

                # 2) 分支必须存在且未发布（失败不占用操作标识）
                branch = self._conn.execute(
                    "SELECT id, status FROM branches WHERE id=?",
                    (branch_id,)).fetchone()
                if branch is None:
                    raise StoreError(
                        "BRANCH_NOT_FOUND",
                        f"发布目标分支 {branch_id} 不存在", status=404,
                        details={"branch_id": branch_id,
                                 "operation_id": operation_id})
                if branch["status"] != "open":
                    raise StoreError(
                        "BRANCH_ALREADY_PUBLISHED",
                        f"分支 {branch_id} 已发布，不能重复发布",
                        status=409,
                        details={"branch_id": branch_id,
                                 "operation_id": operation_id})

                # 3) 同一事务内重新核对：外部依据仍有效且直接依据未变化
                entries = self._conn.execute(
                    "SELECT * FROM branch_entries WHERE branch_id=? "
                    "ORDER BY seq", (branch_id,)).fetchall()
                edge_rows = self._conn.execute(
                    "SELECT child_entry_id, parent_kind, parent_id "
                    "FROM branch_entry_edges WHERE child_entry_id IN "
                    "(SELECT id FROM branch_entries WHERE branch_id=?) "
                    "ORDER BY child_entry_id, seq", (branch_id,)).fetchall()
                entry_edges: dict[str, list[sqlite3.Row]] = {}
                external_refs: set[str] = set()
                for e in edge_rows:
                    entry_edges.setdefault(e["child_entry_id"], []).append(e)
                    if e["parent_kind"] == "record":
                        external_refs.add(e["parent_id"])

                invalid_basis: list[str] = []
                stale_basis: list[str] = []
                for ref in sorted(external_refs):
                    rec = self._conn.execute(
                        "SELECT status FROM records WHERE id=?",
                        (ref,)).fetchone()
                    if rec is None or rec["status"] != "valid":
                        invalid_basis.append(ref)
                        continue
                    snap = self._conn.execute(
                        "SELECT parent_ids FROM branch_snapshot "
                        "WHERE branch_id=? AND record_id=?",
                        (branch_id, ref)).fetchone()
                    current_parents = [r["parent_id"] for r in self._conn.execute(
                        "SELECT parent_id FROM edges WHERE child_id=? "
                        "ORDER BY seq", (ref,))]
                    if (snap is None
                            or json.loads(snap["parent_ids"]) != current_parents):
                        stale_basis.append(ref)
                if invalid_basis or stale_basis:
                    bad = set(invalid_basis) | set(stale_basis)
                    affected = sorted({
                        e["child_entry_id"] for e in edge_rows
                        if e["parent_kind"] == "record"
                        and e["parent_id"] in bad})
                    raise StoreError(
                        "PUBLISH_CONFLICT",
                        "外部依据在分支创建后已变化："
                        + (f"失效 {', '.join(invalid_basis)}；"
                           if invalid_basis else "")
                        + (f"直接依据变化 {', '.join(stale_basis)}"
                           if stale_basis else "")
                        + "，整次发布已回滚，主谱系未产生任何记录",
                        status=409,
                        details={"branch_id": branch_id,
                                 "invalid_basis_ids": invalid_basis,
                                 "stale_basis_ids": stale_basis,
                                 "affected_entry_ids": affected})

                # 4) 按分支顺序分配正式编号并建立全部引用
                mapping: dict[str, str] = {}
                now = _utcnow()
                for entry in entries:
                    rid = self._allocate_seq_locked("seq", "R")
                    mapping[entry["id"]] = rid
                    self._conn.execute(
                        "INSERT INTO records (id, kind, payload, status, "
                        "invalidated_by, invalidated_at, created_at) "
                        "VALUES (?, ?, ?, 'valid', NULL, NULL, ?)",
                        (rid, entry["kind"], entry["payload"], now))
                    for seq, e in enumerate(entry_edges.get(entry["id"], [])):
                        parent = (e["parent_id"] if e["parent_kind"] == "record"
                                  else mapping[e["parent_id"]])
                        self._conn.execute(
                            "INSERT INTO edges (child_id, parent_id, seq) "
                            "VALUES (?, ?, ?)", (rid, parent, seq))
                    self._conn.execute(
                        "UPDATE branch_entries SET published_record_id=? "
                        "WHERE id=?", (rid, entry["id"]))
                self._conn.execute(
                    "UPDATE branches SET status='published', published_at=?, "
                    "published_op=? WHERE id=?", (now, operation_id, branch_id))

                response = {
                    "operation_id": operation_id,
                    "result": "completed",
                    "replayed": False,
                    "branch_id": branch_id,
                    "mapping": mapping,
                    "record_ids": [mapping[e["id"]] for e in entries],
                }
                self._conn.execute(
                    "INSERT INTO operations (operation_id, kind, "
                    "target_record_id, response_json, created_at) "
                    "VALUES (?, 'publish', ?, ?, ?)",
                    (operation_id, branch_id,
                     json.dumps(response, ensure_ascii=False), now))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        return response

    # ------------------------------------------------------------------ #
    # 完整性自检（验收用）
    # ------------------------------------------------------------------ #
    def assert_invariants(self) -> None:
        """有效记录不得依赖失效记录。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT c.id AS child_id, p.id AS parent_id
                FROM records c
                JOIN edges e ON e.child_id = c.id
                JOIN records p ON p.id = e.parent_id
                WHERE c.status='valid' AND p.status='invalid'
                LIMIT 1
                """).fetchone()
        if row is not None:
            raise AssertionError(
                f"不变量被破坏：有效记录 {row['child_id']} "
                f"依赖失效记录 {row['parent_id']}")
