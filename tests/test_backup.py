"""备份/恢复的验证：在线一致性、完整性校验、轮转、恢复前留退路。"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

TMP = tempfile.mkdtemp()
import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "b.db")
db.reset_init_flag()
import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

import backup
import store

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


print("--- 1. 基本备份 ---")
job = store.create_job("备份测试", {"raw_text": "x"})
jid = job["id"] if isinstance(job, dict) else job
with db.tx(db.connect()) as c:
    for i in range(5):
        c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at)"
                  " VALUES (?,?,?,?,?,?)",
                  ("b%d" % i, jid, "人%d" % i, "done", db.now_iso(), db.now_iso()))

r1 = backup.create(keep=3)
check("备份成功", r1["ok"], r1.get("error"))
check("完整性校验通过", r1["verified"], r1.get("error"))
check("文件存在且非空", os.path.exists(r1["path"]) and r1["bytes"] > 0, r1["bytes"])
check("统计到 5 个候选人", r1["stats"].get("candidates") == 5, r1["stats"])
check("统计到 schema 版本 8", r1["stats"].get("schema_version") == 8, r1["stats"])
check("提示备份含明文敏感数据", "明文姓名" in r1["warning"], r1["warning"])

print("\n--- 2. WAL 一致性：备份里有最新写入 ---")
with db.tx(db.connect()) as c:
    c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at)"
              " VALUES ('late',?,?,?,?,?)",
              (jid, "最后写入", "done", db.now_iso(), db.now_iso()))
r2 = backup.create(keep=3)
check("备份包含备份前刚写入的数据（在线一致）",
      r2["stats"].get("candidates") == 6, r2["stats"])

print("\n--- 3. 轮转 ---")
# 把 r1 挪出轮转范围之外，确保后面恢复用的确实是"5 个候选人"那份快照。
# （之前测试就栽在这里：r1 被轮转删掉后，同名文件又被新备份复用，
#   于是 restore 恢复的是另一份内容，测试却以为发现了恢复 bug。）
keep_dir = os.path.join(TMP, "keep")
os.makedirs(keep_dir, exist_ok=True)
r1_kept = os.path.join(keep_dir, "r1.db")
import shutil
shutil.copy2(r1["path"], r1_kept)

for _ in range(4):
    backup.create(keep=3)
files = backup.list_files()
check("只保留 3 份", len(files) == 3, [f["file"] for f in files])
check("保留的是最新的（新在前）",
      all(files[i]["mtime"] >= files[i + 1]["mtime"] for i in range(len(files) - 1)),
      [f["mtime"] for f in files])
names = [f["file"] for f in files]
check("备份文件名互不重复", len(set(names)) == len(names), names)

# 文件名复用的回归检查：连续快速创建，名字必须全不相同
fast = [os.path.basename(backup.create(keep=99)["path"]) for _ in range(6)]
check("同一秒内连续备份也不重名", len(set(fast)) == len(fast), fast)

print("\n--- 4. 拒绝无确认的恢复 ---")
try:
    backup.restore(r1_kept)
    check("无确认应报错", False)
except ValueError as e:
    check("拒绝无确认的恢复", "确认" in str(e), e)

print("\n--- 5. 恢复：先留退路，再覆盖 ---")
before = db.connect().execute("SELECT COUNT(*) n FROM candidates").fetchone()["n"]
res = backup.restore(r1_kept, confirm=True)
check("恢复成功", res["ok"], res)
check("恢复前自动做了安全备份", res["safety_ok"] is True, res)
check("安全备份文件确实存在", os.path.exists(res["safety_backup"]), res["safety_backup"])
after = db.connect().execute("SELECT COUNT(*) n FROM candidates").fetchone()["n"]
check("数据回到 r1 时的 5 个（'late' 不在了）", after == 5, (before, after))
check("'late' 确实没了", db.connect().execute(
    "SELECT COUNT(*) n FROM candidates WHERE id='late'").fetchone()["n"] == 0)
check("恢复动作进了审计", any(a["action"] == "backup.restore" for a in
                              db.connect().execute(
                                  "SELECT action FROM audit").fetchall()))

print("\n--- 6. 坏备份被拒绝 ---")
bad = os.path.join(TMP, "corrupt.db")
with open(bad, "wb") as f:
    f.write(b"this is not a sqlite file at all" * 100)
try:
    backup.restore(bad, confirm=True)
    check("坏备份应被拒绝", False)
except ValueError as e:
    check("坏备份被拒绝（校验不通过）", "校验" in str(e), e)

print("\n--- 7. 自动补备份：新鲜时不重复 ---")
r = backup.auto_backup_if_stale(hours=24)
check("刚备份过 → 跳过", r.get("skipped") is True, r)

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
