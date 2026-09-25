"""分支试建与整组发布：快照、草案条目、发布核对、幂等/冲突、并发与重启。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.store import CalibrationStore, StoreError


@pytest.fixture()
def store(tmp_path):
    db = str(tmp_path / "test.db")
    s = CalibrationStore(db)
    yield s
    s.close()


def _raw(store, value):
    return store.create_record("raw", {"value": value}, None)


# --------------------------------------------------------------------- #
# 分支创建与快照
# --------------------------------------------------------------------- #
def test_branch_snapshot_captures_valid_records_with_direct_basis(store):
    r1 = _raw(store, "raw-1")
    r2 = store.create_record("derived", {"value": "d2"}, [r1["id"]])
    gone = _raw(store, "raw-gone")
    store.invalidate("op-pre", gone["id"])

    branch = store.create_branch()
    assert branch["id"] == "B000001"
    assert branch["status"] == "open"
    # 快照只含当时有效记录，且保存其直接依据
    assert branch["snapshot_record_ids"] == [r1["id"], r2["id"]]
    assert branch["entries"] == []

    # 快照是稳定的：之后主谱系变化不影响快照内容
    store.invalidate("op-post", r1["id"])
    again = store.get_branch(branch["id"])
    assert again["snapshot_record_ids"] == [r1["id"], r2["id"]]


def test_branch_ids_have_own_sequence(store):
    b1 = store.create_branch()
    b2 = store.create_branch()
    assert (b1["id"], b2["id"]) == ("B000001", "B000002")
    assert [b["id"] for b in store.list_branches()] == ["B000001", "B000002"]
    summary = store.list_branches()[0]
    assert summary["entry_count"] == 0 and summary["snapshot_size"] == 0


# --------------------------------------------------------------------- #
# 草案条目
# --------------------------------------------------------------------- #
def test_entries_reference_snapshot_and_prior_entries(store):
    r1 = _raw(store, "raw-1")
    branch = store.create_branch()
    bid = branch["id"]

    e1 = store.create_entry(bid, "raw", {"value": "trial-raw"}, None)
    assert e1["id"] == "D000001"
    e2 = store.create_entry(bid, "derived", {"value": "trial-derived"},
                            [r1["id"], e1["id"]])
    assert e2["id"] == "D000002"
    assert e2["parents"] == [
        {"kind": "record", "id": r1["id"]},
        {"kind": "entry", "id": e1["id"]},
    ]
    # 草案编号与正式编号不同命名空间，且草案不进入正式谱系
    assert [r["id"] for r in store.list_records()] == [r1["id"]]


def test_entry_may_reference_snapshot_record_invalidated_afterwards(store):
    """分支内推导引用快照记录：创建后该记录失效，草案仍可建立（发布时再核对）。"""
    r1 = _raw(store, "raw-1")
    bid = store.create_branch()["id"]
    store.invalidate("op-late", r1["id"])
    entry = store.create_entry(bid, "derived", {"value": "x"}, [r1["id"]])
    assert entry["parents"] == [{"kind": "record", "id": r1["id"]}]


def test_entry_rejects_record_created_after_branch(store):
    bid = store.create_branch()["id"]
    later = _raw(store, "later")
    with pytest.raises(StoreError) as ei:
        store.create_entry(bid, "derived", {"value": "x"}, [later["id"]])
    assert ei.value.code == "BASIS_NOT_IN_SNAPSHOT"
    assert ei.value.details["record_ids"] == [later["id"]]
    assert ei.value.details["branch_id"] == bid


def test_entry_rejects_record_invalid_at_snapshot_time(store):
    gone = _raw(store, "gone")
    store.invalidate("op-gone", gone["id"])
    bid = store.create_branch()["id"]  # 快照不含已失效记录
    with pytest.raises(StoreError) as ei:
        store.create_entry(bid, "derived", {"value": "x"}, [gone["id"]])
    assert ei.value.code == "BASIS_NOT_IN_SNAPSHOT"


def test_entry_rejects_cross_branch_reference(store):
    b1 = store.create_branch()["id"]
    b2 = store.create_branch()["id"]
    foreign = store.create_entry(b1, "raw", {"value": "f"}, None)
    with pytest.raises(StoreError) as ei:
        store.create_entry(b2, "derived", {"value": "x"}, [foreign["id"]])
    assert ei.value.code == "ENTRY_NOT_IN_BRANCH"
    assert ei.value.details["foreign_entry_ids"] == [foreign["id"]]


def test_entry_validation_errors(store):
    bid = store.create_branch()["id"]
    with pytest.raises(StoreError) as ei:
        store.create_entry(bid, "derived", {"value": "x"}, ["D000999"])
    assert ei.value.code == "PARENT_NOT_FOUND"
    assert ei.value.details["missing_parent_ids"] == ["D000999"]

    with pytest.raises(StoreError) as ei:
        store.create_entry(bid, "raw", {"value": "x"}, ["R000001"])
    assert ei.value.code == "RAW_RECORD_HAS_PARENTS"

    with pytest.raises(StoreError) as ei:
        store.create_entry(bid, "derived", {"value": "x"}, [])
    assert ei.value.code == "DERIVED_RECORD_WITHOUT_BASIS"

    with pytest.raises(StoreError) as ei:
        store.create_entry(bid, "derived", {"value": "x"}, ["SELF"],
                           entry_id="SELF")
    assert ei.value.code == "SELF_REFERENCE"

    e1 = store.create_entry(bid, "raw", {"value": "y"}, None)
    with pytest.raises(StoreError) as ei:
        store.create_entry(bid, "derived", {"value": "z"},
                           [e1["id"], e1["id"]])
    assert ei.value.code == "DUPLICATE_PARENT"

    with pytest.raises(StoreError) as ei:
        store.create_entry("B000999", "raw", {"value": "z"}, None)
    assert ei.value.code == "BRANCH_NOT_FOUND"
    assert ei.value.status == 404


# --------------------------------------------------------------------- #
# 整组发布
# --------------------------------------------------------------------- #
def test_publish_assigns_official_ids_in_branch_order(store):
    r1 = _raw(store, "raw-1")          # R000001
    _raw(store, "raw-2")               # R000002
    bid = store.create_branch()["id"]
    e1 = store.create_entry(bid, "derived", {"value": "t1"}, [r1["id"]])
    e2 = store.create_entry(bid, "raw", {"value": "t2"}, None)
    e3 = store.create_entry(bid, "derived", {"value": "t3"},
                            [e1["id"], e2["id"], r1["id"]])

    res = store.publish_branch("pub-1", bid)
    assert res["result"] == "completed" and res["replayed"] is False
    assert res["mapping"] == {
        e1["id"]: "R000003", e2["id"]: "R000004", e3["id"]: "R000005"}
    assert res["record_ids"] == ["R000003", "R000004", "R000005"]

    # 全部引用已建立：外部依据保留，内部草案引用映射为正式编号
    got = {r["id"]: r for r in store.list_records()}
    assert got["R000003"]["parent_ids"] == [r1["id"]]
    assert got["R000004"]["parent_ids"] == []
    assert got["R000005"]["parent_ids"] == ["R000003", "R000004", r1["id"]]
    assert all(got[r]["status"] == "valid" for r in res["record_ids"])

    # 分支与条目状态更新
    branch = store.get_branch(bid)
    assert branch["status"] == "published"
    assert branch["published_op"] == "pub-1"
    by_entry = {e["id"]: e for e in branch["entries"]}
    assert by_entry[e1["id"]]["published_record_id"] == "R000003"
    assert by_entry[e3["id"]]["published_record_id"] == "R000005"
    store.assert_invariants()


def test_publish_replay_returns_first_mapping(store):
    bid = store.create_branch()["id"]
    e1 = store.create_entry(bid, "raw", {"value": "t"}, None)
    first = store.publish_branch("pub-rep", bid)
    second = store.publish_branch("pub-rep", bid)
    assert second["replayed"] is True
    assert second["mapping"] == first["mapping"] == {e1["id"]: "R000001"}
    # 重放不产生新记录
    assert len(store.list_records()) == 1
    stored = store.get_operation("pub-rep")
    assert stored["result"] == "completed"
    assert stored["mapping"] == first["mapping"]


def test_publish_operation_id_conflicts(store):
    b1 = store.create_branch()["id"]
    b2 = store.create_branch()["id"]
    store.create_entry(b1, "raw", {"value": "a"}, None)
    store.create_entry(b2, "raw", {"value": "b"}, None)
    store.publish_branch("pub-shared", b1)

    # 同一标识改换分支 -> 冲突，且不影响另一分支
    with pytest.raises(StoreError) as ei:
        store.publish_branch("pub-shared", b2)
    assert ei.value.status == 409
    assert ei.value.code == "OPERATION_CONFLICT"
    assert ei.value.details["original_target"] == b1
    assert ei.value.details["conflicting_target"] == b2
    assert store.get_branch(b2)["status"] == "open"

    # 与失效裁决共用同一标识空间
    r1 = _raw(store, "victim")
    store.invalidate("op-mixed", r1["id"])
    with pytest.raises(StoreError) as ei:
        store.publish_branch("op-mixed", b2)
    assert ei.value.code == "OPERATION_CONFLICT"


def test_publish_twice_with_different_operation_rejected(store):
    bid = store.create_branch()["id"]
    store.create_entry(bid, "raw", {"value": "t"}, None)
    store.publish_branch("pub-first", bid)
    with pytest.raises(StoreError) as ei:
        store.publish_branch("pub-second", bid)
    assert ei.value.code == "BRANCH_ALREADY_PUBLISHED"
    assert ei.value.status == 409
    # 已发布分支不能再追加草案
    with pytest.raises(StoreError) as ei:
        store.create_entry(bid, "raw", {"value": "late"}, None)
    assert ei.value.code == "BRANCH_CLOSED"


def test_publish_missing_branch_is_locatable_and_op_not_consumed(store):
    with pytest.raises(StoreError) as ei:
        store.publish_branch("pub-ghost", "B000999")
    assert ei.value.status == 404
    assert ei.value.code == "BRANCH_NOT_FOUND"
    assert store.get_operation("pub-ghost") is None
    bid = store.create_branch()["id"]
    res = store.publish_branch("pub-ghost", bid)  # 标识可继续使用
    assert res["replayed"] is False


def test_publish_conflict_rolls_back_everything(store):
    r1 = _raw(store, "basis")          # R000001
    keep = _raw(store, "keep")         # R000002
    bid = store.create_branch()["id"]
    e1 = store.create_entry(bid, "derived", {"value": "t1"}, [r1["id"]])
    e2 = store.create_entry(bid, "derived", {"value": "t2"}, [e1["id"]])
    e3 = store.create_entry(bid, "raw", {"value": "t3"}, None)

    # 分支创建后外部依据失效 -> 整次发布冲突
    store.invalidate("op-kill-basis", r1["id"])
    before = store.list_records()
    with pytest.raises(StoreError) as ei:
        store.publish_branch("pub-conflict", bid)
    err = ei.value
    assert err.status == 409
    assert err.code == "PUBLISH_CONFLICT"
    # 可定位：失效依据与受影响草案条目
    assert err.details["invalid_basis_ids"] == [r1["id"]]
    assert err.details["affected_entry_ids"] == [e1["id"]]
    assert err.details["branch_id"] == bid

    # 主谱系不产生部分记录：记录集合与编号序列均未变
    assert store.list_records() == before
    nxt = _raw(store, "after")
    assert nxt["id"] == "R000003"
    # 分支未发布、条目未获得正式编号、操作标识未占用
    branch = store.get_branch(bid)
    assert branch["status"] == "open"
    assert all(e["published_record_id"] is None for e in branch["entries"])
    assert store.get_operation("pub-conflict") is None
    store.assert_invariants()


def test_publish_conflict_on_transitive_basis_invalidated(store):
    """快照记录的下游（间接依据链）被级联失效同样拦截发布。"""
    r1 = _raw(store, "root")
    r2 = store.create_record("derived", {"value": "mid"}, [r1["id"]])
    bid = store.create_branch()["id"]
    store.create_entry(bid, "derived", {"value": "uses-mid"}, [r2["id"]])
    store.invalidate("op-root", r1["id"])  # 级联使 r2 失效
    with pytest.raises(StoreError) as ei:
        store.publish_branch("pub-x", bid)
    assert ei.value.details["invalid_basis_ids"] == [r2["id"]]


def test_published_records_join_lineage_and_cascade(store):
    """发布后记录进入正式谱系：其依据被裁决时级联失效照常生效。"""
    r1 = _raw(store, "basis")
    bid = store.create_branch()["id"]
    e1 = store.create_entry(bid, "derived", {"value": "t"}, [r1["id"]])
    res = store.publish_branch("pub-cascade", bid)
    official = res["mapping"][e1["id"]]

    inv = store.invalidate("op-after-publish", r1["id"])
    cascaded = {c["id"] for c in inv["cascade"]}
    assert cascaded == {r1["id"], official}
    assert store.get_record(official)["invalidated_by"] == r1["id"]
    store.assert_invariants()


def test_publish_empty_branch_is_noop(store):
    bid = store.create_branch()["id"]
    res = store.publish_branch("pub-empty", bid)
    assert res["mapping"] == {} and res["record_ids"] == []
    assert store.get_branch(bid)["status"] == "published"


# --------------------------------------------------------------------- #
# 并发竞争
# --------------------------------------------------------------------- #
def test_concurrent_publish_and_invalidate_never_orphans(store):
    """发布与失效裁决竞争：最终不存在有效正式记录依赖失效依据。"""
    pivot = _raw(store, "pivot")["id"]
    branches = [store.create_branch()["id"] for _ in range(4)]
    for i, bid in enumerate(branches):
        store.create_entry(bid, "derived", {"value": f"t-{i}"}, [pivot])
    errors: list[Exception] = []

    def worker(i: int):
        try:
            if i % 2 == 0:
                store.publish_branch(f"pub-race-{i}", branches[(i // 2) % 4])
            else:
                store.invalidate(f"op-race-{i}", pivot)
        except StoreError:
            pass  # 冲突/重复是合法结局，关键是不变量
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(worker, range(40)))
    assert not errors
    store.assert_invariants()
    # pivot 失效后，所有依赖它的已发布记录必须一并失效
    records = {r["id"]: r for r in store.list_records()}
    if records[pivot]["status"] == "invalid":
        dependents = [r for r in records.values() if pivot in r["parent_ids"]]
        assert all(r["status"] == "invalid" for r in dependents)


def test_concurrent_competing_branch_publishes(store):
    """两个分支竞争发布同一外部依据：成功者记录有效，或依据已失效则整体冲突。"""
    pivot = _raw(store, "pivot")["id"]
    b1 = store.create_branch()["id"]
    b2 = store.create_branch()["id"]
    store.create_entry(b1, "derived", {"value": "b1"}, [pivot])
    store.create_entry(b2, "derived", {"value": "b2"}, [pivot])
    errors: list[Exception] = []

    def worker(i: int):
        try:
            if i % 3 == 0:
                store.invalidate(f"op-comp-{i}", pivot)
            elif i % 3 == 1:
                store.publish_branch("pub-comp-1", b1)
            else:
                store.publish_branch("pub-comp-2", b2)
        except StoreError:
            pass
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(worker, range(48)))
    assert not errors
    store.assert_invariants()
    records = {r["id"]: r for r in store.list_records()}
    for rec in records.values():
        if rec["status"] == "valid":
            for p in rec["parent_ids"]:
                assert records[p]["status"] == "valid"


def test_concurrent_same_branch_publish_single_winner(store):
    bid = store.create_branch()["id"]
    store.create_entry(bid, "raw", {"value": "t"}, None)

    def invoke(i: int):
        try:
            return store.publish_branch(f"pub-single-{i % 3}", bid)
        except StoreError as e:
            return e

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(invoke, range(36)))
    completed = [r for r in results if not isinstance(r, StoreError)]
    # 只有一个操作标识能真正发布；其余要么重放首次结果，要么冲突
    winners = {r["operation_id"] for r in completed}
    assert len(winners) == 1
    assert all(r["mapping"] == completed[0]["mapping"] for r in completed)
    assert len(store.list_records()) == 1


# --------------------------------------------------------------------- #
# 重启持久化
# --------------------------------------------------------------------- #
def test_branch_state_persists_across_reopen(tmp_path):
    db = str(tmp_path / "persist.db")
    s1 = CalibrationStore(db)
    r1 = _raw(s1, "basis")
    bid = s1.create_branch()["id"]
    e1 = s1.create_entry(bid, "derived", {"value": "t"}, [r1["id"]])
    res = s1.publish_branch("pub-persist", bid)
    s1.close()

    s2 = CalibrationStore(db)  # 重启
    branch = s2.get_branch(bid)
    assert branch["status"] == "published"
    assert branch["entries"][0]["published_record_id"] == res["mapping"][e1["id"]]
    assert branch["snapshot_record_ids"] == [r1["id"]]
    replay = s2.publish_branch("pub-persist", bid)
    assert replay["replayed"] is True
    assert replay["mapping"] == res["mapping"]
    assert s2.get_record(res["mapping"][e1["id"]])["parent_ids"] == [r1["id"]]
    s2.close()
