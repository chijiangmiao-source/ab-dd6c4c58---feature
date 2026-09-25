"""标定谱系存储层。

核心不变量（均在单个 SQLite 写事务内保证）：

1. 一条失效裁决使目标记录及全部可达下游记录在同一持久化提交中失效；
2. 操作标识幂等：重复裁决返回首次结果；同一操作标识改换目标 -> 冲突且不改状态；
3. 新建推导记录时，任一前序不存在 / 已失效 / 自引用 / 成环 -> 整笔拒绝，既有结论不变；
4. 写事务串行化（BEGIN IMMEDIATE），因此“新推导”与“失效裁决”竞争后，
   不可能存在有效记录依赖失效记录。

分支试建 / 整组发布：

5. 创建分支时冻结当时全部可引用有效记录（稳定快照，含直接依据指纹）；
   分支内草案只能引用快照记录或本分支先前草案，不影响正式谱系；
6. 发布在单个持久化提交内重新核对全部外部依据：仍存在、仍有效、
   直接依据指纹与快照一致；任一不满足 -> 整次发布返回可定位冲突，
   正式谱系不产生任何部分记录；
7. 校验通过才按分支内顺序分配正式编号并一次建立全部引用；
   发布操作标识幂等（重传返回首次编号映射），改换分支 -> 冲突。
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
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL CHECK (status IN ('draft', 'published', 'abandoned')),
    created_at  TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS branch_snapshot (
    branch_id   TEXT NOT NULL REFERENCES branches(id),
    record_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    basis_fingerprint TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    PRIMARY KEY (branch_id, record_id)
);
CREATE TABLE IF NOT EXISTS branch_entries (
    id          TEXT PRIMARY KEY,
    branch_id   TEXT NOT NULL REFERENCES branches(id),
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('raw', 'derived')),
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (branch_id, seq)
);
CREATE TABLE IF NOT EXISTS branch_entry_parents (
    entry_id   TEXT NOT NULL REFERENCES branch_entries(id),
    branch_id  TEXT NOT NULL REFERENCES branches(id),
    parent_ref TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    PRIMARY KEY (entry_id, parent_ref)
);
CREATE INDEX IF NOT EXISTS idx_branch_entries_branch ON branch_entries(branch_id);
CREATE INDEX IF NOT EXISTS idx_branch_parents_branch ON branch_entry_parents(branch_id);
CREATE TABLE IF NOT EXISTS branch_published_records (
    branch_id TEXT NOT NULL REFERENCES branches(id),
    entry_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (branch_id, entry_id)
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


def _draft_id_of(entry_pk: str) -> str:
    """branch_pk 'B000001:D000003' -> 草案编号 'D000003'。"""
    return entry_pk.rsplit(":", 1)[1]


def _split_fingerprint(fingerprint: str) -> list[str]:
    return fingerprint.split("|") if fingerprint else []


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
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='seq'").fetchone()
        seq = (int(row["value"]) + 1) if row else 1
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(seq),))
        return f"R{seq:06d}"

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
    # 分支试建
    # ------------------------------------------------------------------ #
    def create_branch(self, branch_id: str | None = None) -> dict[str, Any]:
        """创建试建分支，并冻结当时全部可引用有效记录的稳定快照。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                bid = branch_id or self._allocate_branch_id_locked()
                if self._conn.execute(
                        "SELECT 1 FROM branches WHERE id=?", (bid,)).fetchone():
                    raise StoreError("BRANCH_ID_CONFLICT",
                                     f"分支编号 {bid} 已存在", status=409,
                                     details={"branch_id": bid})
                now = _utcnow()
                self._conn.execute(
                    "INSERT INTO branches (id, status, created_at, "
                    "published_at) VALUES (?, 'draft', ?, NULL)", (bid, now))
                # 稳定快照：冻结正式谱系中当前有效记录及其直接依据指纹
                valid_rows = self._conn.execute(
                    "SELECT id FROM records WHERE status='valid' "
                    "ORDER BY created_at, id").fetchall()
                for seq, r in enumerate(valid_rows):
                    fp = self._basis_fingerprint_locked(r["id"])
                    self._conn.execute(
                        "INSERT INTO branch_snapshot (branch_id, record_id, "
                        "status, basis_fingerprint, seq) VALUES (?, ?, "
                        "'valid', ?, ?)", (bid, r["id"], fp, seq))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_branch(bid)

    def list_branches(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM branches ORDER BY created_at, id").fetchall()
            snap_counts = {r["branch_id"]: r["n"] for r in self._conn.execute(
                "SELECT branch_id, COUNT(*) AS n FROM branch_snapshot "
                "GROUP BY branch_id").fetchall()}
            entry_counts = {r["branch_id"]: r["n"] for r in self._conn.execute(
                "SELECT branch_id, COUNT(*) AS n FROM branch_entries "
                "GROUP BY branch_id").fetchall()}
        return [{
            "id": r["id"],
            "status": r["status"],
            "created_at": r["created_at"],
            "published_at": r["published_at"],
            "snapshot_count": snap_counts.get(r["id"], 0),
            "entry_count": entry_counts.get(r["id"], 0),
        } for r in rows]

    def get_branch(self, branch_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM branches WHERE id=?", (branch_id,)).fetchone()
            if row is None:
                raise StoreError("BRANCH_NOT_FOUND",
                                 f"分支 {branch_id} 不存在", status=404,
                                 details={"branch_id": branch_id})
            snap_rows = self._conn.execute(
                "SELECT record_id, status, basis_fingerprint FROM "
                "branch_snapshot WHERE branch_id=? ORDER BY seq",
                (branch_id,)).fetchall()
            entry_rows = self._conn.execute(
                "SELECT id, seq, kind, payload FROM branch_entries "
                "WHERE branch_id=? ORDER BY seq", (branch_id,)).fetchall()
            parent_rows = self._conn.execute(
                "SELECT entry_id, parent_ref FROM branch_entry_parents "
                "WHERE branch_id=? ORDER BY entry_id, seq",
                (branch_id,)).fetchall()
            published_rows = self._conn.execute(
                "SELECT entry_id, record_id FROM branch_published_records "
                "WHERE branch_id=?", (branch_id,)).fetchall()
        parents: dict[str, list[str]] = {}
        for p in parent_rows:
            parents.setdefault(p["entry_id"], []).append(p["parent_ref"])
        published = {r["entry_id"]: r["record_id"] for r in published_rows}
        return {
            "id": row["id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            "snapshot": [{
                "record_id": s["record_id"],
                "status": s["status"],
                "direct_basis": _split_fingerprint(s["basis_fingerprint"]),
            } for s in snap_rows],
            "entries": [{
                "draft_id": _draft_id_of(e["id"]),
                "seq": e["seq"],
                "kind": e["kind"],
                "payload": json.loads(e["payload"]),
                "parent_refs": parents.get(e["id"], []),
                "record_id": published.get(e["id"]),
            } for e in entry_rows],
        }

    def add_branch_entry(self, branch_id: str, kind: str,
                         payload: dict[str, Any],
                         parent_refs: list[str] | None) -> dict[str, Any]:
        """在分支内追加一条草案；依据只能来自快照或本分支先前草案。"""
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
                if branch["status"] != "draft":
                    raise StoreError(
                        "BRANCH_NOT_DRAFT",
                        f"分支 {branch_id} 状态为 {branch['status']}，"
                        "不能再追加草案", status=409,
                        details={"branch_id": branch_id,
                                 "status": branch["status"]})

                if kind == "raw" and parent_refs:
                    raise StoreError(
                        "RAW_RECORD_HAS_PARENTS",
                        "原始标定记录不能引用前序记录", status=400,
                        details={"parent_refs": parent_refs})
                if kind == "derived" and not parent_refs:
                    raise StoreError(
                        "DERIVED_RECORD_WITHOUT_BASIS",
                        "推导标定记录必须至少选择一个可引用依据")
                if len(set(parent_refs)) != len(parent_refs):
                    dup = sorted({p for p in parent_refs
                                  if parent_refs.count(p) > 1})
                    raise StoreError("DUPLICATE_PARENT",
                                     "前序记录重复出现",
                                     details={"duplicate_parent_ids": dup})

                snap_ids = {r["record_id"] for r in self._conn.execute(
                    "SELECT record_id FROM branch_snapshot WHERE branch_id=?",
                    (branch_id,)).fetchall()}
                prior_drafts = {_draft_id_of(r["id"]) for r in
                                self._conn.execute(
                                    "SELECT id FROM branch_entries "
                                    "WHERE branch_id=?",
                                    (branch_id,)).fetchall()}

                seq_row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS m FROM branch_entries "
                    "WHERE branch_id=?", (branch_id,)).fetchone()
                seq = seq_row["m"] + 1
                draft_id = f"D{seq:06d}"

                if draft_id in parent_refs:
                    raise StoreError(
                        "SELF_REFERENCE",
                        f"草案 {draft_id} 不能引用自身作为前序依据",
                        details={"draft_id": draft_id,
                                 "parent_refs": parent_refs})

                missing = [p for p in parent_refs
                           if p not in snap_ids and p not in prior_drafts]
                if missing:
                    raise StoreError(
                        "PARENT_NOT_FOUND",
                        f"前序记录 {', '.join(missing)} 不在分支快照中，"
                        "也不是本分支先前草案",
                        details={"missing_parent_ids": missing,
                                 "branch_id": branch_id})
                # 引用本分支后续草案不可能（seq 递增），成环只需向上探测
                if self._draft_reaches_locked(
                        branch_id, draft_id,
                        {p for p in parent_refs if p in prior_drafts}):                    raise StoreError(
                        "CYCLE_DETECTED",
                        "分支草案引用关系形成环",
                        details={"draft_id": draft_id,
                                 "parent_refs": parent_refs})

                entry_pk = f"{branch_id}:{draft_id}"
                self._conn.execute(
                    "INSERT INTO branch_entries (id, branch_id, seq, kind, "
                    "payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (entry_pk, branch_id, seq, kind,
                     json.dumps(payload, ensure_ascii=False), _utcnow()))
                for pseq, ref in enumerate(parent_refs):
                    self._conn.execute(
                        "INSERT INTO branch_entry_parents (entry_id, "
                        "branch_id, parent_ref, seq) VALUES (?, ?, ?, ?)",
                        (entry_pk, branch_id, ref, pseq))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_branch_entry(branch_id, draft_id)

    def get_branch_entry(self, branch_id: str, draft_id: str) -> dict[str, Any]:
        with self._lock:
            return self._get_entry_locked(branch_id, draft_id)

    def _get_entry_locked(self, branch_id: str, draft_id: str) -> dict[str, Any]:
        entry_pk = f"{branch_id}:{draft_id}"
        row = self._conn.execute(
            "SELECT id, branch_id, seq, kind, payload FROM branch_entries "
            "WHERE id=?", (entry_pk,)).fetchone()
        if row is None:
            raise StoreError("BRANCH_ENTRY_NOT_FOUND",
                             f"分支 {branch_id} 中无草案 {draft_id}",
                             status=404,
                             details={"branch_id": branch_id,
                                      "draft_id": draft_id})
        refs = [r["parent_ref"] for r in self._conn.execute(
            "SELECT parent_ref FROM branch_entry_parents WHERE entry_id=? "
            "ORDER BY seq", (entry_pk,)).fetchall()]
        pub = self._conn.execute(
            "SELECT record_id FROM branch_published_records WHERE entry_id=?",
            (entry_pk,)).fetchone()
        return {
            "draft_id": draft_id,
            "branch_id": branch_id,
            "seq": row["seq"],
            "kind": row["kind"],
            "payload": json.loads(row["payload"]),
            "parent_refs": refs,
            "record_id": pub["record_id"] if pub else None,
        }

    def _draft_reaches_locked(self, branch_id: str, target: str,
                              starts: set[str]) -> bool:
        """分支内从 starts 沿 parent_ref 向上能否到达 target 草案。

        仅沿“本分支草案 -> 本分支草案”的边上升；外部快照记录（即使自定义
        编号以 D 开头）不属于分支草案，不作为上升通道。
        """
        frontier = set(starts)
        seen: set[str] = set()
        while frontier:
            if target in frontier:
                return True
            seen |= frontier
            rows = self._conn.execute(
                "SELECT bep.parent_ref AS parent_ref "
                "FROM branch_entry_parents bep "
                "JOIN branch_entries be "
                "  ON be.id = ? || ':' || bep.parent_ref "
                "WHERE bep.branch_id=? AND bep.entry_id IN "
                f"({','.join('?' * len(frontier))})",
                (branch_id, branch_id,
                 *[f"{branch_id}:{d}" for d in frontier])).fetchall()
            frontier = {r["parent_ref"] for r in rows} - seen
        return False

    def _basis_fingerprint_locked(self, record_id: str) -> str:
        """记录直接依据的稳定指纹：按 seq 排序的直接父节点拼接。"""
        rows = self._conn.execute(
            "SELECT parent_id FROM edges WHERE child_id=? ORDER BY seq",
            (record_id,)).fetchall()
        return "|".join(r["parent_id"] for r in rows)

    def _allocate_branch_id_locked(self) -> str:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='branch_seq'").fetchone()
        seq = (int(row["value"]) + 1) if row else 1
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('branch_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(seq),))
        return f"B{seq:06d}"

    # ------------------------------------------------------------------ #
    # 分支整组发布（单事务复核 + 按序分配正式编号 + 建立全部引用）
    # ------------------------------------------------------------------ #
    def publish_branch(self, operation_id: str,
                       branch_id: str) -> dict[str, Any]:
        if not operation_id:
            raise StoreError("OPERATION_ID_REQUIRED",
                             "分支发布必须携带操作标识", status=400)

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                response = self._publish_branch_txn_locked(
                    operation_id, branch_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return response

    def _publish_branch_txn_locked(self, operation_id: str,
                                   branch_id: str) -> dict[str, Any]:
        # 1) 操作标识幂等 / 冲突
        op = self._conn.execute(
            "SELECT kind, target_record_id, response_json FROM operations "
            "WHERE operation_id=?", (operation_id,)).fetchone()
        if op is not None:
            if op["kind"] == "publish_branch" \
                    and op["target_record_id"] == branch_id:
                response = json.loads(op["response_json"])
                response["replayed"] = True
                return response
            raise StoreError(
                "OPERATION_CONFLICT",
                f"操作标识 {operation_id} 已用于 "
                f"{op['kind']}({op['target_record_id']})，"
                f"不能改用于 publish_branch({branch_id})",
                status=409,
                details={"operation_id": operation_id,
                         "original_target": op["target_record_id"],
                         "original_kind": op["kind"],
                         "conflicting_branch_id": branch_id})

        # 2) 分支必须存在且仍为草案
        branch = self._conn.execute(
            "SELECT id, status FROM branches WHERE id=?",
            (branch_id,)).fetchone()
        if branch is None:
            raise StoreError("BRANCH_NOT_FOUND",
                             f"分支 {branch_id} 不存在", status=404,
                             details={"branch_id": branch_id,
                                      "operation_id": operation_id})
        if branch["status"] != "draft":
            raise StoreError(
                "BRANCH_NOT_DRAFT",
                f"分支 {branch_id} 已 {branch['status']}，不能重复发布",
                status=409,
                details={"branch_id": branch_id, "status": branch["status"]})

        entries = self._conn.execute(
            "SELECT id, seq, kind, payload FROM branch_entries "
            "WHERE branch_id=? ORDER BY seq", (branch_id,)).fetchall()
        if not entries:
            raise StoreError("BRANCH_EMPTY",
                             f"分支 {branch_id} 没有任何草案，整组发布无对象",
                             details={"branch_id": branch_id})
        ref_rows = self._conn.execute(
            "SELECT entry_id, parent_ref FROM branch_entry_parents "
            "WHERE branch_id=? ORDER BY entry_id, seq",
            (branch_id,)).fetchall()
        refs_by_entry: dict[str, list[str]] = {}
        for r in ref_rows:
            refs_by_entry.setdefault(r["entry_id"], []).append(r["parent_ref"])
        # 本分支全部草案编号：同名时（自定义正式编号恰为 D…）草案引用优先
        own_draft_ids = {_draft_id_of(e["id"]) for e in entries}

        snap = {r["record_id"]: r for r in self._conn.execute(
            "SELECT record_id, basis_fingerprint FROM branch_snapshot "
            "WHERE branch_id=?", (branch_id,)).fetchall()}

        # 3) 同一提交内重新核对全部外部依据（分支创建时冻结的快照记录）：
        #    必须仍存在、仍有效、直接依据指纹与创建分支时一致
        current = {r["id"]: r for r in self._conn.execute(
            "SELECT id, status FROM records").fetchall()}
        conflicts: list[dict[str, Any]] = []
        for e in entries:
            for ref in refs_by_entry.get(e["id"], []):
                if ref in own_draft_ids:
                    continue  # 本分支先前草案，无需外部复核
                snap_row = snap.get(ref)
                cur = current.get(ref)
                item = {"entry_id": _draft_id_of(e["id"]), "parent_id": ref}
                if snap_row is None or cur is None:
                    item["reason"] = "not_found"
                    conflicts.append(item)
                elif cur["status"] != "valid":
                    item["reason"] = "invalid"
                    item["current_status"] = cur["status"]
                    conflicts.append(item)
                else:
                    fp_now = self._basis_fingerprint_locked(ref)
                    if fp_now != snap_row["basis_fingerprint"]:
                        item["reason"] = "basis_changed"
                        item["snapshot_fingerprint"] = \
                            snap_row["basis_fingerprint"]
                        item["current_fingerprint"] = fp_now
                        conflicts.append(item)
        if conflicts:
            raise StoreError(
                "PUBLISH_CONFLICT",
                f"分支 {branch_id} 的外部依据已失效或自快照后发生变化，"
                "整次发布被拒绝，正式谱系不产生任何记录",
                status=409,
                details={"branch_id": branch_id,
                         "operation_id": operation_id,
                         "conflicts": conflicts})

        # 4) 校验通过：按分支顺序一次分配正式编号并建立全部引用
        start_seq = self._next_main_seq_locked()
        draft_to_formal: dict[str, str] = {}
        now = _utcnow()
        for offset, e in enumerate(entries):
            formal_id = f"R{start_seq + offset:06d}"
            draft_to_formal[_draft_id_of(e["id"])] = formal_id

        for e in entries:
            formal_id = draft_to_formal[_draft_id_of(e["id"])]
            self._conn.execute(
                "INSERT INTO records (id, kind, payload, status, "
                "invalidated_by, invalidated_at, created_at) "
                "VALUES (?, ?, ?, 'valid', NULL, NULL, ?)",
                (formal_id, e["kind"], e["payload"], now))
            for pseq, ref in enumerate(refs_by_entry.get(e["id"], [])):
                parent_formal = draft_to_formal.get(ref, ref)
                self._conn.execute(
                    "INSERT INTO edges (child_id, parent_id, seq) "
                    "VALUES (?, ?, ?)", (formal_id, parent_formal, pseq))
            self._conn.execute(
                "INSERT INTO branch_published_records (branch_id, entry_id, "
                "record_id, seq) VALUES (?, ?, ?, ?)",
                (branch_id, e["id"], formal_id, e["seq"]))

        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(start_seq + len(entries) - 1),))
        self._conn.execute(
            "UPDATE branches SET status='published', published_at=? "
            "WHERE id=?", (now, branch_id))

        mapping = [{
            "draft_id": _draft_id_of(e["id"]),
            "record_id": draft_to_formal[_draft_id_of(e["id"])],
        } for e in entries]
        response = {
            "operation_id": operation_id,
            "result": "published",
            "replayed": False,
            "branch_id": branch_id,
            "mapping": mapping,
        }
        self._conn.execute(
            "INSERT INTO operations (operation_id, kind, target_record_id, "
            "response_json, created_at) VALUES (?, 'publish_branch', ?, ?, ?)",
            (operation_id, branch_id,
             json.dumps(response, ensure_ascii=False), now))
        return response

    def _next_main_seq_locked(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='seq'").fetchone()
        return (int(row["value"]) + 1) if row else 1

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
