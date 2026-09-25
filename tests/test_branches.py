"""CalibrationStore 分支试建 / 整组发布测试。

覆盖：稳定快照、草案编号与正式编号区分、单事务发布复核、
外部依据失效 / 直接依据变化 -> 整次拒绝且主谱系无部分记录、
发布操作标识幂等与改换分支冲突、竞争发布不变量、重启持久化。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.store import CalibrationStore, StoreError


@pytest.fixture()
def store(tmp_path):
    db = str(tmp_path / "branch.db")
    s = CalibrationStore(db)
    yield s
    s.close()


def test_branch_freezes_snapshot_of_current_valid_records(store):
    r1 = store.create_record("raw", {"value": "a"}, None)
    r2 = store.create_record("derived", {"value": "b"}, [r1["id"]])
    branch = store.create_branch()
    assert branch["id"] == "B000001"
    assert branch["status"] == "draft"
    assert [s["record_id"] for s in branch["snapshot"]] == [r1["id"], r2["id"]]
    # 快照冻结直接依据指纹
    assert branch["snapshot"][1]["direct_basis"] == [r1["id"]]

    # 创建分支后正式谱系的新记录不进入快照，也不可被草案引用
    r3 = store.create_record("raw", {"value": "c"}, None)
    with pytest.raises(StoreError) as ei:
        store.add_branch_entry(branch["id"], "derived", {"value": "x"},
                               [r3["id"]])
    assert ei.value.code == "PARENT_NOT_FOUND"
    assert ei.value.details["missing_parent_ids"] == [r3["id"]]


def test_draft_ids_distinct_from_formal_ids_and_do_not_touch_main_lineage(store):
    r1 = store.create_record("raw", {"value": "a"}, None)
    branch = store.create_branch()
    before = {r["id"] for r in store.list_records()}

    d1 = store.add_branch_entry(branch["id"], "raw", {"value": "draft-raw"},
                                None)
    d2 = store.add_branch_entry(
        branch["id"], "derived", {"value": "draft-derived"},
        [r1["id"], d1["draft_id"]])
    assert d1["draft_id"] == "D000001" and d1["record_id"] is None
    assert d2["draft_id"] == "D000002"
    assert d2["parent_refs"] == [r1["id"], "D000001"]

    # 正式谱系完全不受草案影响
    after = {r["id"] for r in store.list_records()}
    assert after == before
    got = store.get_branch(branch["id"])
    assert [e["draft_id"] for e in got["entries"]] == ["D000001", "D000002"]


def test_branch_entry_validation(store):
    r1 = store.create_record("raw", {"value": "a"}, None)
    branch = store.create_branch()

    with pytest.raises(StoreError) as ei:
        store.add_branch_entry(branch["id"], "derived", {"value": "x"},
                               ["R000999"])
    assert ei.value.code == "PARENT_NOT_FOUND"
    assert ei.value.details["missing_parent_ids"] == ["R000999"]

    with pytest.raises(StoreError) as ei:
        store.add_branch_entry(branch["id"], "raw", {"value": "x"},
                               [r1["id"]])
    assert ei.value.code == "RAW_RECORD_HAS_PARENTS"

    with pytest.raises(StoreError) as ei:
        store.add_branch_entry(branch["id"], "derived", {"value": "x"}, [])
    assert ei.value.code == "DERIVED_RECORD_WITHOUT_BASIS"

    with pytest.raises(StoreError) as ei:
        store.add_branch_entry(branch["id"], "derived", {"value": "x"},
                               [r1["id"], r1["id"]])
    assert ei.value.code == "DUPLICATE_PARENT"


def test_entries_rejected_on_missing_or_published_branch(store):
    with pytest.raises(StoreError) as ei:
        store.add_branch_entry("B000999", "raw", {"value": "x"}, None)
    assert ei.value.status == 404
    assert ei.value.code == "BRANCH_NOT_FOUND"

    r1 = store.create_record("raw", {"value": "a"}, None)
    branch = store.create_branch()
    store.add_branch_entry(branch["id"], "derived", {"value": "x"}, [r1["id"]])
    store.publish_branch("pub-1", branch["id"])
    with pytest.raises(StoreError) as ei:
        store.add_branch_entry(branch["id"], "raw", {"value": "y"}, None)
    assert ei.value.code == "BRANCH_NOT_DRAFT"


def test_publish_assigns_formal_ids_in_branch_order_with_all_edges(store):
    r1 = store.create_record("raw", {"value": "a"}, None)
    r2 = store.create_record("raw", {"value": "b"}, None)  # R000002
    branch = store.create_branch()
    bid = branch["id"]
    store.add_branch_entry(bid, "raw", {"value": "d1"}, None)
    store.add_branch_entry(bid, "derived", {"value": "d2"},
                           [r1["id"], "D000001"])
    store.add_branch_entry(bid, "derived", {"value": "d3"},
                           ["D000002", r2["id"]])

    result = store.publish_branch("pub-1", bid)
    assert result["replayed"] is False
    assert [m["record_id"] for m in result["mapping"]] == \
        ["R000003", "R000004", "R000005"]
    assert [m["draft_id"] for m in result["mapping"]] == \
        ["D000001", "D000002", "D000003"]

    assert store.get_record("R000004")["parent_ids"] == [r1["id"], "R000003"]
    assert store.get_record("R000005")["parent_ids"] == ["R000004", r2["id"]]
    # 主序列推进，后续单条创建继续编号
    nxt = store.create_record("raw", {"value": "e"}, None)
    assert nxt["id"] == "R000006"
    # 分支状态与编号映射可查
    got = store.get_branch(bid)
    assert got["status"] == "published"
    assert [e["record_id"] for e in got["entries"]] == \
        ["R000003", "R000004", "R000005"]
    store.assert_invariants()


def test_publish_rejected_when_external_basis_invalidated(store):
    r1 = store.create_record("raw", {"value": "a"}, None)
    r2 = store.create_record("derived", {"value": "b"}, [r1["id"]])
    store.create_branch("B1")
    store.add_branch_entry("B1", "raw", {"value": "d1"}, None)
    store.add_branch_entry("B1", "derived", {"value": "d2"}, [r2["id"]])

    # 创建分支后外部依据被失效裁决（r2 随 r1 级联失效）
    store.invalidate("op-kill", r1["id"])
    main_ids_before = {r["id"] for r in store.list_records()}

    with pytest.raises(StoreError) as ei:
        store.publish_branch("pub-1", "B1")
    err = ei.value
    assert err.status == 409
    assert err.code == "PUBLISH_CONFLICT"
    conflicts = err.details["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["entry_id"] == "D000002"
    assert conflicts[0]["parent_id"] == r2["id"]
    assert conflicts[0]["reason"] == "invalid"

    # 主谱系不产生任何部分记录；分支仍为草案，可查冲突
    assert {r["id"] for r in store.list_records()} == main_ids_before
    assert store.get_branch("B1")["status"] == "draft"
    # 操作标识未被失败发布占用，修正（重建分支）后可用于成功发布
    store.create_branch("B2")
    store.add_branch_entry("B2", "raw", {"value": "ok"}, None)
    result = store.publish_branch("pub-1", "B2")
    assert result["result"] == "published"


def test_publish_rejected_when_direct_basis_changed_after_snapshot(store):
    r1 = store.create_record("raw", {"value": "a"}, None)
    r2 = store.create_record("raw", {"value": "b"}, None)
    r3 = store.create_record("derived", {"value": "c"}, [r1["id"]])
    store.create_branch("B1")
    store.add_branch_entry("B1", "derived", {"value": "d"}, [r3["id"]])

    # 直接依据被外部改动（遗留边场景）：快照指纹与当前不一致
    with store._lock:
        store._conn.execute("BEGIN IMMEDIATE")
        store._conn.execute(
            "INSERT INTO edges(child_id, parent_id, seq) VALUES (?, ?, ?)",
            (r3["id"], r2["id"], 1))
        store._conn.execute("COMMIT")

    with pytest.raises(StoreError) as ei:
        store.publish_branch("pub-1", "B1")
    assert ei.value.code == "PUBLISH_CONFLICT"
    conflict = ei.value.details["conflicts"][0]
    assert conflict["parent_id"] == r3["id"]
    assert conflict["reason"] == "basis_changed"
    assert conflict["snapshot_fingerprint"] == r1["id"]
    assert conflict["current_fingerprint"] == f"{r1['id']}|{r2['id']}"
    # 主谱系无新增
    assert all(r["id"] != "R000004" for r in store.list_records())


def test_publish_empty_branch_rejected(store):
    store.create_branch("B1")
    with pytest.raises(StoreError) as ei:
        store.publish_branch("pub-1", "B1")
    assert ei.value.code == "BRANCH_EMPTY"


def test_publish_idempotent_replay_returns_first_mapping(store):
    r1 = store.create_record("raw", {"value": "a"}, None)
    store.create_branch("B1")
    store.add_branch_entry("B1", "derived", {"value": "d"}, [r1["id"]])
    first = store.publish_branch("pub-x", "B1")
    second = store.publish_branch("pub-x", "B1")
    third = store.publish_branch("pub-x", "B1")
    assert first["mapping"] == second["mapping"] == third["mapping"]
    assert first["replayed"] is False
    assert second["replayed"] is True and third["replayed"] is True
    # 重放不产生重复正式记录
    assert len(store.list_records()) == 2
    # 也可经操作标识查询首次结果
    assert store.get_operation("pub-x")["mapping"] == first["mapping"]


def test_publish_same_op_different_branch_conflicts(store):
    store.create_record("raw", {"value": "a"}, None)
    store.create_branch("B1")
    store.create_branch("B2")
    store.add_branch_entry("B1", "raw", {"value": "1"}, None)
    store.add_branch_entry("B2", "raw", {"value": "2"}, None)
    store.publish_branch("shared-op", "B1")
    with pytest.raises(StoreError) as ei:
        store.publish_branch("shared-op", "B2")
    assert ei.value.status == 409
    assert ei.value.code == "OPERATION_CONFLICT"
    assert ei.value.details["original_target"] == "B1"
    assert ei.value.details["conflicting_branch_id"] == "B2"
    # B2 未被发布
    assert store.get_branch("B2")["status"] == "draft"


def test_publish_op_conflicts_with_invalidate_op(store):
    r1 = store.create_record("raw", {"value": "a"}, None)
    store.create_branch("B1")
    store.add_branch_entry("B1", "raw", {"value": "1"}, None)
    store.invalidate("shared-op", r1["id"])
    with pytest.raises(StoreError) as ei:
        store.publish_branch("shared-op", "B1")
    assert ei.value.code == "OPERATION_CONFLICT"
    assert ei.value.details["original_kind"] == "invalidate"


def test_concurrent_branch_publish_vs_invalidation_never_orphans(store):
    """分支发布与失效裁决竞争后：
    不存在有效正式记录依赖失效记录，也不存在有效记录依赖过期快照依据。"""
    pivot = store.create_record("raw", {"value": "pivot"}, None)["id"]
    errors: list[Exception] = []

    def make_branch(i: int):
        bid = f"B{i:04d}"
        store.create_branch(bid)
        store.add_branch_entry(bid, "derived", {"value": f"d-{i}"}, [pivot])
        return bid

    branch_ids = [make_branch(i) for i in range(1, 21)]

    def worker(i: int):
        try:
            if i % 4 == 0:
                store.invalidate(f"op-inv-{i}", pivot)
            else:
                store.publish_branch(f"op-pub-{i}", branch_ids[i])
        except StoreError:
            pass  # 冲突 / 失效失败都是合法结局
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(worker, range(20)))
    assert not errors
    store.assert_invariants()

    # 凡发布成功且其依据 pivot 已失效的正式记录，必随级联失效
    by_id = {r["id"]: r for r in store.list_records()}
    pivot_invalid = by_id[pivot]["status"] == "invalid"
    for r in by_id.values():
        if r["status"] == "valid":
            assert pivot not in r["parent_ids"]
    if pivot_invalid:
        children = [r for r in by_id.values() if pivot in r["parent_ids"]]
        assert all(c["status"] == "invalid" for c in children)


def test_two_branches_competing_publish_leave_no_stale_dependency(store):
    """两个分支竞争发布；之后再裁决依据失效，过期快照依据不得支撑有效记录。"""
    pivot = store.create_record("raw", {"value": "pivot"}, None)["id"]

    def prep(tag):
        store.create_branch(tag)
        store.add_branch_entry(tag, "derived", {"value": tag}, [pivot])
        return tag

    b1, b2 = prep("B1"), prep("B2")

    def pub(tag):
        try:
            return store.publish_branch(f"op-{tag}", tag)
        except StoreError as e:
            return e

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(pub, [b1, b2] * 8))
    # 每分支只允许发布一次；其余同标识重放
    published = [r for r in results if not isinstance(r, StoreError)]
    assert published
    for r in published:
        assert r["result"] == "published"

    store.invalidate("op-kill-pivot", pivot)
    store.assert_invariants()
    for rec in store.list_records():
        if pivot in rec["parent_ids"]:
            assert rec["status"] == "invalid"


def test_external_record_with_draft_like_id_resolves_as_snapshot(store):
    """自定义正式编号恰为 D… 时：快照依据按外部记录处理，不与本分支草案混淆。"""
    ext = store.create_record("raw", {"value": "ext"}, None,
                              record_id="D000999")
    store.create_branch("B1")
    store.add_branch_entry(
        "B1", "derived", {"value": "use-ext"}, [ext["id"]])  # D000001
    store.add_branch_entry(
        "B1", "derived", {"value": "use-own-draft"}, ["D000001"])  # D000002
    result = store.publish_branch("pub-1", "B1")
    mapping = {m["draft_id"]: m["record_id"] for m in result["mapping"]}
    # 第一条草案依赖外部自定义编号记录；第二条依赖本分支第一条
    assert store.get_record(mapping["D000001"])["parent_ids"] == ["D000999"]
    assert store.get_record(mapping["D000002"])["parent_ids"] == \
        [mapping["D000001"]]
    store.assert_invariants()


def test_branch_persistence_after_reopen(tmp_path):
    db = str(tmp_path / "branch-persist.db")
    s1 = CalibrationStore(db)
    r1 = s1.create_record("raw", {"value": "a"}, None)
    s1.create_branch("B1")
    s1.add_branch_entry("B1", "derived", {"value": "d"}, [r1["id"]])
    result = s1.publish_branch("pub-restart", "B1")
    s1.close()

    s2 = CalibrationStore(db)  # 重启
    branch = s2.get_branch("B1")
    assert branch["status"] == "published"
    assert branch["entries"][0]["record_id"] == "R000002"
    # 发布操作重放返回首次编号映射
    replay = s2.publish_branch("pub-restart", "B1")
    assert replay["replayed"] is True
    assert replay["mapping"] == result["mapping"]
    # 快照仍可查
    assert [s["record_id"] for s in branch["snapshot"]] == [r1["id"]]
    s2.close()
