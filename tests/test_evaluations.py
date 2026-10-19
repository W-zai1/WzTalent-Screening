"""迁移 7：evaluations 表 + 历史回填。"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

TMP = tempfile.mkdtemp()
import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "m7.db")
db.reset_init_flag()
import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


db.init()
conn = db.connect()
print("schema_version =", db.SCHEMA_VERSION)
check("迁移到 v8", db.SCHEMA_VERSION == 8)
ver = conn.execute("SELECT MAX(version) v FROM schema_migrations").fetchone()["v"]
check("schema_migrations 记录到 8", ver == 8, ver)

tables = {r["name"] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
check("evaluations 表存在", "evaluations" in tables, sorted(tables))

idx = {r["name"] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='index'")}
for i in ("idx_eval_cand", "idx_eval_job", "idx_eval_ver"):
    check("索引 %s 存在" % i, i in idx)

cols = {r["name"] for r in conn.execute("PRAGMA table_info(evaluations)")}
need = {"id", "candidate_id", "job_id", "run_id", "total", "grade",
        "rubric_version", "prompt_fingerprint", "model", "provider",
        "repeats", "stdev", "result_json", "created_at"}
check("字段齐全", need <= cols, sorted(need - cols))

print("\n--- 历史回填 ---")
import store
job = store.create_job("回填测试", {"raw_text": "x"})
jid = job["id"] if isinstance(job, dict) else job
with db.tx(conn) as c:
    c.execute(
        "INSERT INTO candidates(id, job_id, name, status, created_at, updated_at, "
        "result_json, rubric_version) VALUES (?,?,?,?,?,?,?,?)",
        ("cbf1", jid, "张三", "done", db.now_iso(), db.now_iso(),
         db.jdump({"total": 8.1, "grade": "A",
                   "rubric_parts": {"prompt_fingerprint": "abc123"}}), "rOLD"))
n = conn.execute("SELECT COUNT(*) n FROM evaluations WHERE candidate_id='cbf1'"
                 ).fetchone()["n"]
# 回填发生在迁移时，这条候选人是迁移之后插的，所以不该有历史——
# 正确行为是 **0**，历史由后续评审写入。
check("迁移后才插入的数据不会被误回填", n == 0, n)

print("\n--- ON DELETE CASCADE ---")
with db.tx(conn) as c:
    c.execute("INSERT INTO evaluations(id, candidate_id, job_id, total, created_at)"
              " VALUES ('ev1','cbf1',?,8.1,?)", (jid, db.now_iso()))
check("可插入历史", conn.execute(
    "SELECT COUNT(*) n FROM evaluations WHERE candidate_id='cbf1'").fetchone()["n"] == 1)
conn.execute("PRAGMA foreign_keys=ON")
with db.tx(conn) as c:
    c.execute("DELETE FROM candidates WHERE id='cbf1'")
n = conn.execute("SELECT COUNT(*) n FROM evaluations WHERE candidate_id='cbf1'"
                 ).fetchone()["n"]
check("删候选人时历史一并清除（外键 CASCADE）", n == 0, n)

print()
print("失败 %d 项" % len(FAIL))
sys.exit(0 if not FAIL else 1)
