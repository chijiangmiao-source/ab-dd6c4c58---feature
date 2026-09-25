#!/usr/bin/env python3
"""一次性验收服务 verify。

在低温探测器标定谱系这一真实业务场景下依次执行：

  1. 代码测试：pytest 全量单元/接口用例（级联失效、幂等/冲突、引用校验、
     并发竞争、重启持久化）；
  2. 构建检查：compileall 语法构建 + 应用可导入；
  3. API/HTTP 冒烟：拉起真实 gunicorn 服务（4 worker，跨进程竞争），
     经 HTTP 复现稳定编号、级联失效、操作重放、同标识换目标冲突、
     可定位错误反馈、并发竞争不变量，以及重启后谱系/失效状态/操作重放；
  4. 可选：若设置 VERIFY_TARGET_URL，则对已运行的服务（如 compose 中的
     web 服务）追加一次真实 HTTP 冒烟。

全部步骤通过则退出码 0，任一失败退出码 1。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = "PASS"
FAIL = "FAIL"


class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []
        self.ok = True

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.items.append((PASS if ok else FAIL, name, detail))
        self.ok = self.ok and ok
        print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        self.step(name, bool(cond), detail)
        return bool(cond)


def wait_for_port(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def wait_healthy(base_url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=2)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return True
        except requests.RequestException:
            pass
        time.sleep(0.3)
    return False


class Server:
    """真实 gunicorn 子进程（4 worker，制造跨进程写竞争）。"""

    def __init__(self, db_path: str, port: int, workers: int = 4):
        self.db_path = db_path
        self.port = port
        self.workers = workers
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = os.environ.copy()
        env["CALIBRATION_DB"] = self.db_path
        env["PYTHONPATH"] = str(ROOT)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "gunicorn",
             f"--workers={self.workers}",
             f"--bind=127.0.0.1:{self.port}",
             "--timeout=30",
             "app.server:create_app()"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        if not wait_for_port("127.0.0.1", self.port):
            out = self.proc.stdout.read() if self.proc.stdout else ""
            raise RuntimeError(f"服务未在端口 {self.port} 就绪\n{out}")
        if not wait_healthy(f"http://127.0.0.1:{self.port}"):
            raise RuntimeError("健康端点未返回 ok")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None


# --------------------------------------------------------------------------- #
# 阶段 1/2：代码测试 + 构建检查
# --------------------------------------------------------------------------- #
def run_code_phase(rep: Report) -> None:
    print("\n=== 阶段 1：代码测试（pytest） ===")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(ROOT),
    )
    rep.step("pytest 全量用例", proc.returncode == 0,
             "退出码 0" if proc.returncode == 0 else f"退出码 {proc.returncode}")

    print("\n=== 阶段 2：构建检查 ===")
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "app", "scripts"],
        cwd=str(ROOT))
    rep.step("compileall 语法构建", proc.returncode == 0)

    proc = subprocess.run(
        [sys.executable, "-c",
         "from app.server import create_app; create_app().test_client(); "
         "print('factory ok')"],
        cwd=str(ROOT), capture_output=True, text=True)
    rep.step("应用工厂可导入并构造", proc.returncode == 0,
             proc.stdout.strip() or proc.stderr.strip()[-300:])


# --------------------------------------------------------------------------- #
# 阶段 3：自管服务的 HTTP 全链路冒烟（含重启）
# --------------------------------------------------------------------------- #
def _post(base: str, path: str, body: dict) -> requests.Response:
    return requests.post(f"{base}{path}", json=body, timeout=10)


def run_self_hosted_phase(rep: Report, workdir: Path) -> None:
    print("\n=== 阶段 3：API/HTTP 冒烟（真实 gunicorn x4 worker） ===")
    db_path = str(workdir / "verify.db")
    port = int(os.environ.get("VERIFY_PORT", "18080"))
    base = f"http://127.0.0.1:{port}"
    server = Server(db_path, port)
    server.start()
    rep.step("服务启动且 /health 可访问", True, f"{base}/health")
    try:
        _http_lifecycle(rep, base)
        _http_error_cases(rep, base)
        _http_branch_workflow(rep, base)
        _http_concurrency(rep, base)
        _http_branch_publish_race(rep, base)
    finally:
        server.stop()

    print("\n=== 阶段 4：重启后谱系 / 失效状态 / 操作重放 ===")
    server2 = Server(db_path, port, workers=2)
    server2.start()
    try:
        _http_restart_persistence(rep, base)
        _http_branch_restart_persistence(rep, base)
    finally:
        server2.stop()


def _http_lifecycle(rep: Report, base: str) -> None:
    a = _post(base, "/api/records",
              {"kind": "raw", "payload": {"value": "77K/A"}}).json()
    b = _post(base, "/api/records",
              {"kind": "raw", "payload": {"value": "4K/B"}}).json()
    c = _post(base, "/api/records", {
        "kind": "derived", "payload": {"value": "gain"},
        "parent_ids": [a["id"], b["id"]]}).json()
    d = _post(base, "/api/records", {
        "kind": "derived", "payload": {"value": "offset"},
        "parent_ids": [c["id"]]}).json()
    rep.check("稳定编号按序分配",
              [x["id"] for x in (a, b, c, d)] ==
              ["R000001", "R000002", "R000003", "R000004"])
    rep.check("提交即展示有效性与直接依据",
              c["status"] == "valid" and c["parent_ids"] == [a["id"], b["id"]])

    inv = _post(base, f"/api/records/{a['id']}/invalidate",
                {"operation_id": "verify-op-cascade"}).json()
    cascaded = {x["id"] for x in inv["cascade"]}
    rep.check("裁决在一次提交内级联到全部可达下游",
              cascaded == {a["id"], c["id"], d["id"]},
              f"cascade={sorted(cascaded)}")

    records = {r["id"]: r for r in requests.get(f"{base}/api/records").json()}
    rep.check("失效来源稳定且指向裁决目标",
              all(records[i]["invalidated_by"] == a["id"]
                  for i in (a["id"], c["id"], d["id"])))
    rep.check("无关节联结论保持有效", records[b["id"]]["status"] == "valid")

    again = _post(base, f"/api/records/{a['id']}/invalidate",
                  {"operation_id": "verify-op-cascade"}).json()
    rep.check("重复同一裁决返回首次结果（replayed）",
              again.get("replayed") is True
              and {x["id"] for x in again["cascade"]} == cascaded)

    conflict = _post(base, f"/api/records/{b['id']}/invalidate",
                     {"operation_id": "verify-op-cascade"})
    b_after = requests.get(f"{base}/api/records/{b['id']}").json()
    rep.check("同一操作标识改换目标 -> 409 且状态不变",
              conflict.status_code == 409
              and conflict.json()["error"]["code"] == "OPERATION_CONFLICT"
              and b_after["status"] == "valid")


def _http_error_cases(rep: Report, base: str) -> None:
    r = _post(base, "/api/records", {
        "kind": "derived", "payload": {}, "parent_ids": ["R000999"]})
    rep.check("引用不存在记录：拒绝并给出可定位反馈",
              r.status_code == 422
              and r.json()["error"]["details"]["missing_parent_ids"] == ["R000999"])

    r = _post(base, "/api/records", {
        "kind": "derived", "record_id": "LOOP",
        "payload": {}, "parent_ids": ["LOOP"]})
    rep.check("自引用：拒绝（SELF_REFERENCE），既有结论不变",
              r.status_code == 422
              and r.json()["error"]["code"] == "SELF_REFERENCE")

    r = _post(base, "/api/records", {
        "kind": "derived", "payload": {}, "parent_ids": ["R000001"]})
    rep.check("引用已失效记录：拒绝（PARENT_INVALID）",
              r.status_code == 422
              and r.json()["error"]["code"] == "PARENT_INVALID")

    r = _post(base, "/api/records/GHOST/invalidate",
              {"operation_id": "verify-op-ghost"})
    rep.check("裁决不存在记录：404 可定位",
              r.status_code == 404
              and r.json()["error"]["details"]["record_id"] == "GHOST")


def _http_branch_workflow(rep: Report, base: str) -> None:
    """分支试建 -> 复核 -> 整组发布全链路（真实 HTTP）。"""
    # 外部有效依据（此时编号承接前面的冒烟，取实际返回值）
    a = _post(base, "/api/records",
              {"kind": "raw", "payload": {"value": "branch-base-a"}}).json()
    b = _post(base, "/api/records",
              {"kind": "raw", "payload": {"value": "branch-base-b"}}).json()
    before_count = len(requests.get(f"{base}/api/records").json())

    br = _post(base, "/api/branches", {"branch_id": "BR-1"})
    rep.check("创建分支返回 201", br.status_code == 201, br.text[:200])
    branch = br.json()
    snap_ids = {s["record_id"] for s in branch["snapshot"]}
    rep.check("分支保存当时有效记录的稳定快照",
              a["id"] in snap_ids and b["id"] in snap_ids)

    d1 = _post(base, "/api/branches/BR-1/entries",
               {"kind": "raw", "payload": {"value": "draft-1"}}).json()
    d2 = _post(base, "/api/branches/BR-1/entries", {
        "kind": "derived", "payload": {"value": "draft-2"},
        "parent_refs": [a["id"], d1["draft_id"]]}).json()
    rep.check("草案编号与正式编号明确区分（D… 且无正式编号）",
              d1["draft_id"] == "D000001" and d1["record_id"] is None
              and d2["draft_id"] == "D000002"
              and d2["parent_refs"] == [a["id"], "D000001"])
    rep.check("草案不写入正式谱系",
              len(requests.get(f"{base}/api/records").json()) == before_count)

    # 快照外的记录不能引用
    ghost = _post(base, "/api/branches/BR-1/entries", {
        "kind": "derived", "payload": {"value": "x"},
        "parent_refs": ["R000999"]})
    rep.check("分支引用快照外记录：拒绝并可定位",
              ghost.status_code == 422
              and ghost.json()["error"]["code"] == "PARENT_NOT_FOUND"
              and ghost.json()["error"]["details"]
              ["missing_parent_ids"] == ["R000999"])

    # 第二个分支：依据 a 创建草案后，让 a 失效，发布必须整组冲突
    _post(base, "/api/branches", {"branch_id": "BR-STALE"})
    _post(base, "/api/branches/BR-STALE/entries", {
        "kind": "derived", "payload": {"value": "stale"},
        "parent_refs": [a["id"]]})
    inv = _post(base, f"/api/records/{a['id']}/invalidate",
                {"operation_id": "verify-branch-kill-a"}).json()
    rep.check("失效裁决按既有链路级联（含已发布记录）",
              a["id"] in {x["id"] for x in inv["cascade"]})

    bad_pub = _post(base, "/api/branches/BR-STALE/publish",
                    {"operation_id": "verify-pub-stale"})
    rep.check("外部依据失效：整次发布 409 且冲突可定位",
              bad_pub.status_code == 409
              and bad_pub.json()["error"]["code"] == "PUBLISH_CONFLICT"
              and bad_pub.json()["error"]["details"]["conflicts"] == [{
                  "entry_id": "D000001", "parent_id": a["id"],
                  "reason": "invalid", "current_status": "invalid"}],
              bad_pub.text[:300])
    rep.check("失败发布在主谱系不产生部分记录",
              len(requests.get(f"{base}/api/records").json()) == before_count)
    rep.check("冲突后分支仍是草案",
              requests.get(f"{base}/api/branches/BR-STALE").json()
              ["status"] == "draft")

    # BR-1 的草案依赖 a（已失效）→ 同样整组冲突
    bad_pub2 = _post(base, "/api/branches/BR-1/publish",
                     {"operation_id": "verify-pub-br1"})
    rep.check("BR-1 同样因失效依据被整组拒绝",
              bad_pub2.status_code == 409
              and bad_pub2.json()["error"]["code"] == "PUBLISH_CONFLICT")

    # 新建一个只引用仍有效依据 b 的分支，发布成功并按序给正式编号
    _post(base, "/api/branches", {"branch_id": "BR-OK"})
    e1 = _post(base, "/api/branches/BR-OK/entries",
               {"kind": "raw", "payload": {"value": "ok-draft-1"}}).json()
    e2 = _post(base, "/api/branches/BR-OK/entries", {
        "kind": "derived", "payload": {"value": "ok-draft-2"},
        "parent_refs": [b["id"], "D000001"]}).json()
    pub = _post(base, "/api/branches/BR-OK/publish",
                {"operation_id": "verify-pub-ok"})
    rep.check("复核通过：整组发布 200", pub.status_code == 200, pub.text[:200])
    mapping = pub.json()["mapping"]
    formal = {m["draft_id"]: m["record_id"] for m in mapping}
    expect = {e1["draft_id"], e2["draft_id"]}
    rep.check("发布按分支顺序分配正式编号并给出映射",
              set(formal) == expect and len(set(formal.values())) == 2
              and [m["draft_id"] for m in mapping] == ["D000001", "D000002"])
    f2 = requests.get(
        f"{base}/api/records/{formal['D000002']}").json()
    rep.check("发布建立全部引用（外部依据 + 同分支先前条目）",
              f2["parent_ids"] == [b["id"], formal["D000001"]]
              and f2["status"] == "valid",
              f"f2={f2}")

    # 同标识重传：返回首次编号映射
    replay = _post(base, "/api/branches/BR-OK/publish",
                   {"operation_id": "verify-pub-ok"}).json()
    rep.check("相同发布标识重传返回首次编号映射",
              replay.get("replayed") is True
              and replay["mapping"] == mapping)

    # 标识改换分支 -> 冲突
    swap = _post(base, "/api/branches/BR-STALE/publish",
                 {"operation_id": "verify-pub-ok"})
    rep.check("发布标识改换分支 -> 409 OPERATION_CONFLICT",
              swap.status_code == 409
              and swap.json()["error"]["code"] == "OPERATION_CONFLICT"
              and swap.json()["error"]["details"]
              ["original_target"] == "BR-OK")

    # 页面可经真实接口观察到分支与发布后的正式谱系
    listed = {x["id"]: x for x in
              requests.get(f"{base}/api/branches").json()}
    rep.check("分支列表可观察：BR-OK 已发布、BR-STALE 仍草案",
              listed["BR-OK"]["status"] == "published"
              and listed["BR-STALE"]["status"] == "draft"
              and listed["BR-OK"]["entry_count"] == 2)
    ok_detail = requests.get(f"{base}/api/branches/BR-OK").json()
    rep.check("分支详情含正式编号映射",
              {e["record_id"] for e in ok_detail["entries"]}
              == set(formal.values()))


def _http_branch_publish_race(rep: Report, base: str) -> None:
    """分支发布与失效裁决跨进程并发竞争：不得留下对失效/过期依据的有效依赖。"""
    pivot = _post(base, "/api/records",
                  {"kind": "raw", "payload": {"value": "branch-pivot"}}
                  ).json()["id"]
    for i in range(10):
        _post(base, "/api/branches", {"branch_id": f"BR-RACE-{i}"})
        _post(base, f"/api/branches/BR-RACE-{i}/entries", {
            "kind": "derived", "payload": {"value": f"r{i}"},
            "parent_refs": [pivot]})

    def one(i: int) -> None:
        s = requests.Session()
        try:
            if i % 4 == 0:
                s.post(f"{base}/api/records/{pivot}/invalidate",
                       json={"operation_id": f"verify-branch-inv-{i}"},
                       timeout=15)
            else:
                s.post(f"{base}/api/branches/BR-RACE-{i}/publish",
                       json={"operation_id": f"verify-branch-pub-{i}"},
                       timeout=15)
        except requests.RequestException as exc:
            raise AssertionError(str(exc)) from exc

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(one, range(10)))

    records = requests.get(f"{base}/api/records").json()
    by_id = {r["id"]: r for r in records}
    bad = [r["id"] for r in records
           if r["status"] == "valid"
           and any(by_id.get(p, {}).get("status") == "invalid"
                   for p in r["parent_ids"])]
    rep.check("分支竞争发布后不存在有效记录依赖失效/过期依据", not bad,
              f"违规记录：{bad}")
    if by_id[pivot]["status"] == "invalid":
        children = [r for r in records if pivot in r["parent_ids"]]
        rep.check("竞争中失效闭包完整：pivot 下游全部失效",
                  all(c["status"] == "invalid" for c in children),
                  f"children={len(children)}")


def _http_branch_restart_persistence(rep: Report, base: str) -> None:
    branch = requests.get(f"{base}/api/branches/BR-OK").json()
    rep.check("重启后分支状态 / 快照 / 编号映射仍可查",
              branch["status"] == "published"
              and all(e["record_id"] for e in branch["entries"])
              and any(s["record_id"] for s in branch["snapshot"]))
    replay = _post(base, "/api/branches/BR-OK/publish",
                   {"operation_id": "verify-pub-ok"}).json()
    rep.check("重启后发布操作重放返回首次编号映射",
              replay.get("replayed") is True
              and {m["draft_id"] for m in replay["mapping"]} ==
              {"D000001", "D000002"})


def _http_concurrency(rep: Report, base: str) -> None:
    """新推导 vs 失效裁决 跨进程并发竞争。"""
    pivot = _post(base, "/api/records",
                  {"kind": "raw", "payload": {"value": "pivot"}}).json()["id"]
    # 预置多层有效下游，确保级联闭包在竞争中被真正检验
    pre1 = _post(base, "/api/records", {
        "kind": "derived", "payload": {"value": "pre1"},
        "parent_ids": [pivot]}).json()["id"]
    pre2 = _post(base, "/api/records", {
        "kind": "derived", "payload": {"value": "pre2"},
        "parent_ids": [pre1]}).json()["id"]
    errors: list[str] = []

    def one(i: int) -> None:
        s = requests.Session()
        try:
            if i % 4 == 0:
                s.post(f"{base}/api/records/{pivot}/invalidate",
                       json={"operation_id": "verify-op-race"}, timeout=15)
            else:
                s.post(f"{base}/api/records", json={
                    "kind": "derived",
                    "payload": {"value": f"race-{i}"},
                    "parent_ids": [pivot]}, timeout=15)
        except requests.RequestException as exc:
            errors.append(str(exc))

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(one, range(64)))

    rep.check("并发期间无传输层错误", not errors, str(errors))

    records = requests.get(f"{base}/api/records").json()
    by_id = {r["id"]: r for r in records}
    bad = [r["id"] for r in records
           if r["status"] == "valid"
           and any(by_id.get(p, {}).get("status") == "invalid"
                   for p in r["parent_ids"])]
    rep.check("竞争后不存在有效记录依赖失效记录", not bad,
              f"违规记录：{bad}" if bad else "")

    children = [r for r in records if pivot in r["parent_ids"]]
    rep.check("失效裁决闭包完整：pivot 的全部下游均失效",
              by_id[pivot]["status"] == "invalid"
              and all(c["status"] == "invalid" for c in children),
              f"pivot={pivot}, children={len(children)}")


def _http_restart_persistence(rep: Report, base: str) -> None:
    records = {r["id"]: r for r in requests.get(f"{base}/api/records").json()}
    ok = (records["R000001"]["status"] == "invalid"
          and records["R000003"]["status"] == "invalid"
          and records["R000003"]["invalidated_by"] == "R000001"
          and records["R000002"]["status"] == "valid")
    rep.check("重启后谱系与失效状态（含稳定来源）仍可查询", ok)

    replay = _post(base, "/api/records/R000001/invalidate",
                   {"operation_id": "verify-op-cascade"}).json()
    rep.check("重启后操作标识重放返回首次结果",
              replay.get("replayed") is True
              and {x["id"] for x in replay["cascade"]} ==
              {"R000001", "R000003", "R000004"})

    opq = requests.get(f"{base}/api/operations/verify-op-cascade").json()
    rep.check("操作结果可按标识查询", opq["result"] == "completed")


# --------------------------------------------------------------------------- #
# 阶段 5（可选）：对外部已运行服务冒烟（compose 中的 web）
# --------------------------------------------------------------------------- #
def run_external_phase(rep: Report, base_url: str) -> None:
    print(f"\n=== 阶段 5：对外部服务 {base_url} 冒烟 ===")
    tag = f"ext-{os.getpid()}-{int(time.time()*1000)}"
    if not rep.check("外部服务 /health 可访问", wait_healthy(base_url, 15)):
        return
    a = _post(base_url, "/api/records",
              {"kind": "raw", "payload": {"value": tag}}).json()
    b = _post(base_url, "/api/records", {
        "kind": "derived", "payload": {"value": tag + "-d"},
        "parent_ids": [a["id"]]}).json()
    inv = _post(base_url, f"/api/records/{a['id']}/invalidate",
                {"operation_id": f"{tag}-op"}).json()
    got = {x["id"] for x in inv["cascade"]}
    rep.check("外部服务级联失效正确", got == {a["id"], b["id"]},
              f"cascade={sorted(got)}")
    replay = _post(base_url, f"/api/records/{a['id']}/invalidate",
                   {"operation_id": f"{tag}-op"}).json()
    rep.check("外部服务裁决可幂等重放", replay.get("replayed") is True)


def main() -> int:
    print("低温探测器标定谱系 —— 一次性验收 verify")
    print(f"工作目录：{ROOT}")
    rep = Report()
    run_code_phase(rep)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run_self_hosted_phase(rep, Path(tmp))
        except Exception as exc:  # noqa: BLE001
            rep.step("HTTP 冒烟执行", False, f"{type(exc).__name__}: {exc}")
    external = os.environ.get("VERIFY_TARGET_URL")
    if external:
        try:
            run_external_phase(rep, external.rstrip("/"))
        except Exception as exc:  # noqa: BLE001
            rep.step("外部服务冒烟", False, f"{type(exc).__name__}: {exc}")

    print("\n================ 验收汇总 ================")
    for status, name, detail in rep.items:
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    print("==========================================")
    print("验收结果：" + ("全部通过 ✅" if rep.ok else "存在失败 ❌"))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
