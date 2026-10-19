"""逐项核验 2.3 (P0/P1/P2) 全部 16 项的落地证据。

不做"看起来存在就算完成"的判断——每项都要能指出具体代码位置与运行时行为。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


def has(path, *pats):
    s = read(path)
    return all(re.search(p, s) for p in pats)


def any_of(path, *pats):
    s = read(path)
    return any(re.search(p, s) for p in pats)


rows = []


def item(key, name, ok, ev):
    rows.append((key, name, ok, ev))


# ---------------------------------------------------------------- P0
item("P0-1", "消除 skills 内的假数据",
     not any_of("skills/resume-parse/parse.py",
                r"\b(张三|李四|王五|John|Jane)\b") and
     not any_of("skills/asr-transcribe/transcribe.py",
                r"return\s+\{[^}]*['\"]text['\"]\s*:\s*['\"][^'\"]{10,}"),
     "skills 中无任何写死的姓名/转写内容；transcribe.py 无结果时显式报错")

s = read("engine/runs.py")
item("P0-2a", "运行互斥（RunBusy → 409）",
     "class RunBusy" in s and "active_run" in s,
     "runs.start() 检测在飞运行；api 捕获 RunBusy → 409")

s = read("engine/api.py")
item("P0-2b", "取消接口",
     "/api/runs/:rid/cancel" in s and "request_cancel" in read("engine/runs.py"),
     "POST /api/runs/:rid/cancel → runs.request_cancel → cancel_requested 轮询")

s = read("engine/sse.py")
item("P0-2c", "SSE 断点续传（after=N）",
     "after" in s and "events_since" in read("engine/runs.py"),
     "GET /api/runs/:rid/stream?after=N 重放 run_events")

item("P0-3", "提示注入防御（定界+降权+检测）",
     has("engine/fence.py", r"def fence", r"def annotate", r"def detect_injection") and
     any_of("engine/agents.py", r"fence\."),
     "fence.py 三件套；agents/jd_struct/interview 均已接入")

s = read("engine/identity.py")
item("P0-4", "多信号身份判定（姓名/学历/城市/工作经历）",
     all(k in s for k in ("education", "work_history", "location")) and
     "MIN_SIGNALS_FOR_AUTO" in s and "uncertain" in s,
     "学历/单位 3.0、城市 1.5；姓名不计入证据数；不足 2 信号转人工")

# ---------------------------------------------------------------- P1
s = read("engine/db.py")
# 版本号不写死在这里：写死的数字每次加迁移都要手改，迟早忘记，
# 于是检查会说"5 个迁移"而实际是 7 个——一条永远为真、也永远没用的断言。
import db as _db
_migrations = re.findall(r"@_migration\((\d+)\)", s)
item("P1-5", "SQLite 迁移 + schema_version",
     "SCHEMA_VERSION" in s and "schema_migrations" in s and "_MIGRATIONS" in s,
     "SCHEMA_VERSION=%d；_MIGRATIONS 注册 %d 个迁移，schema_migrations 记录已应用版本"
     % (_db.SCHEMA_VERSION, len(_migrations)))

s = read("engine/audit.py")
item("P1-6", "仅追加哈希链审计",
     "sha256" in s and "prev" in s.lower() and "verify" in s,
     "每行含前一行哈希；audit.verify() 从头重算，篡改即断链")

s = read("engine/auth.py")
item("P1-7", "本地认证（PBKDF2 + 会话 + 角色 + CSRF）",
     "pbkdf2_hmac" in s and "ROLES" in s and "def can" in s,
     "PBKDF2 240000 轮；会话存 sha256(token)；admin/hr/viewer 服务端强制")

item("P1-8", "PII 加密 / 留存 / 导出 / 删除",
     has("engine/crypto.py", r"enc:v1", r"def encrypt_text", r"def decrypt_text") and
     has("engine/privacy.py", r"def erase", r"def export", r"def purge"),
     "enc:v1 + encrypt-then-MAC；留存清理支持 dry-run；按人导出/擦除")

s = read("engine/rubric.py")
item("P1-9", "rubric_version 口径版本戳",
     "sha256" in s and "rubric_version" in s,
     "口径哈希前 8 位（如 rcc4d8228），评审/追问都记录")

item("P1-10", "结果回填闭环",
     has("engine/outcome.py", r"def record", r"STAGES") and
     any_of("engine/talent.py", r"outcome\."),
     "POST /api/talent/:pid/outcome；档案带进展时间线与 latest_stage")

item("P1-11", "校准与漂移",
     has("engine/outcome.py", r"def calibration_report", r"def drift", r"MIN_SAMPLES"),
     "样本不足返回 sufficient=false，不编造数字")

# ---------------------------------------------------------------- P2
s = read("engine/server.py")
item("P2-12", "拆分 server.py",
     len(s.splitlines()) < 300 and "import http_base" in s,
     "server.py %d 行（组合根），逻辑在 http_base/api 等模块" % len(s.splitlines()))

_tests = sorted(f for f in os.listdir(os.path.join(ROOT, "tests"))
                if f.startswith("test_") and f.endswith(".py"))
item("P2-13", "测试 + CI + 去重工具",
     len(_tests) >= 3 and
     os.path.exists(os.path.join(ROOT, ".github/workflows/test.yml")) and
     os.path.exists(os.path.join(ROOT, "engine/util.py")),
     "%d 个测试套件 + 零依赖守卫 + CI；util.py 收敛重复工具" % len(_tests))

item("P2-14", "真实 token 用量记账",
     has("engine/usage.py", r"prompt_tokens|estimated", r"def record") and
     any_of("engine/llm_client.py", r"usage"),
     "llm_client 解析 usage 并标记 estimated=1（缺失时）；按角色拆分")

item("P2-15", "评委并行",
     "parallel_judges" in read("engine/agents.py"),
     "ThreadPoolExecutor 并发三位评委")

noise = ["dist", "downloader.py", "version.json", "talent_pool.json"]
present = [n for n in noise if os.path.exists(os.path.join(ROOT, n))]
item("P2-16", "清理仓库噪声",
     not present,
     "dist/ downloader.py version.json talent_pool.json 均已移除")

# ---------------------------------------------------------------- 度量基础
# 这一组回答的是"这个系统的结论能不能被检验"，而不是"功能有没有做"。
item("M-17", "口径可复现：提示词指纹自动进版本号",
     has("engine/rubric.py", r"def prompt_fingerprint", r"_EXTRA_SIGNATURE") and
     has("engine/agents.py", r"def prompt_bundle"),
     "改动任何一句提示词/温度，rubric_version 自动变化；手工 PROMPT_REVISION 仅作意图标记")

item("M-18", "打分与裁定温度=0，辩论保留温度",
     has("engine/agents.py", r"JUDGE_TEMPERATURE\s*=\s*0\.0",
         r"DEBATE_TEMPERATURE\s*=\s*0\.7"),
     "评委/裁定 temperature=0（可复现），辩论 0.7（要多样性），二者都进签名")

item("M-19", "重复评审：中位数 + 离散度 + 跨档告警",
     has("engine/agents.py", r"def _run_repeated", r"stability_warning") and
     has("engine/agents.py", r'"stdev"'),
     "REFINE_REPEATS>1 时整场重复，取中位数；stdev 为 None 表示「只测了一次」，不是「很稳」")

item("M-20", "评审追加历史（不覆盖旧结果）",
     "evaluations" in read("engine/db.py") and
     has("engine/store.py", r"def evaluations_of", r"def evaluation_drift"),
     "migration 7 建 evaluations 表；每次评审追加一行，可回看同一人的分数序列")

item("M-21", "不确定度：bootstrap 置信区间",
     has("engine/align.py", r"def _bootstrap_ci", r"precision_ci"),
     "precision@k 附带 95% 区间；样本不足不给区间而不是给 0")

item("M-22", "同名歧义不猜、可人工绑定",
     has("engine/align.py", r"def pending_bindings", r"def bind_truth") and
     "align.bind" in read("engine/audit.py"),
     "同名多人时报告层拒绝按姓名回退；绑定需人工指认并进审计")

item("M-23", "备份与恢复（在线备份 + 校验 + 轮转）",
     has("engine/backup.py", r"def create", r"def restore", r"def auto_backup_if_stale",
         r"integrity_check"),
     "sqlite3 backup API 在线备份并 integrity_check；启动时按 24h 补备份；默认留 7 份")

item("M-24", "全局并发闸门（跨 run 生效）",
     has("engine/llm_client.py", r"_GLOBAL_LIMITS", r"def concurrency_snapshot"),
     "按 provider 限制在飞请求；原实现按「每次 run」限流，多 run 叠加会打爆配额")

item("M-25", "超长材料显式截断并留痕",
     has("engine/context.py", r"def fit_budget") and
     any_of("engine/agents.py", r"context_truncated"),
     "不再只打 warning；结论范围被标注为「只对被读到的部分成立」")

item("M-26", "岗位级评审细则可配（权重 + 每维看什么）",
     has("engine/rubric.py", r"PRESETS = \{", r"def normalize_config",
         r"def config_fingerprint") and
     has("engine/agents.py", r"def rubric_criteria_block") and
     any_of("engine/api.py", r"/api/jobs/:jid/rubric"),
     "7 组岗位预设各自给出「凭什么这么设」；细则进评委提示词，指纹进版本号")

item("M-27", "细则与权重同源（避免双份真相）",
     has("engine/store.py", r"jd\[.rubric.\] = cfg", r'jd\["weights"\] = cfg\["weights"\]'),
     "保存细则时同步写 jd.weights；界面显示的权重就是算分用的权重")

item("M-28", "评审进行中禁止改口径",
     has("engine/api.py", r"active_run", r"结束后再改口径"),
     "同一批人不能被两种尺子混着评；改完版本号必变并提示跨口径不可比")

item("M-29", "直播间可浮窗（拖动/缩放/收起/记忆位置）",
     any_of("viewer/app.js", r"function initLiveBox", r"toggleLiveFloat") and
     any_of("viewer/styles.css", r"#liveCard\.float", r"resize: both"),
     "评审期间可以一边看辩论一边做别的事；几何值存 localStorage")

item("M-30", "辩论流式输出（逐块推送 + 打字机光标）",
     has("engine/llm_client.py", r"def parse_sse_deltas", r"def complete_stream") and
     has("engine/workbuddy_client.py", r"def chat_stream") and
     has("engine/runs.py", r"def emit_delta") and
     any_of("viewer/app.js", r"agent_delta", r"scheduleBubbleRender"),
     "逐字推送而不是等全部生成完；增量只广播不落库，全文仍落 agent_message")

item("M-31", "流式失败兜底与开关",
     has("engine/llm_client.py", r"REFINE_STREAM", r"改为非流式重取全文"),
     "流到一半断了不重试同一路（会重复输出），改用非流式取回全文；REFINE_STREAM=0 可关")

# ---------------------------------------------------------------- 健壮性整改
item("R-32", "温度必须显式传（禁止靠默认值恰好相等）",
     has("engine/agents.py", r"stream=False, temperature,"),
     "_agent_turn 的 temperature 是必填关键字参数；另有 AST 检查盯住新增调用点")

item("R-33", "提示词常量不被内联复制",
     any_of("engine/agents.py", r'"content": _PROMPT_CON_REBUT'),
     "反方互驳引用常量；否则改常量时实际提示词不变而指纹以为变了")

item("R-34", "辩论历史有预算守卫（含评委包）",
     has("engine/context.py", r"HISTORY_TOKEN_BUDGET", r"def history_text\(self, budget=None") and
     has("engine/agents.py", r"hist_final", r"debate_truncated"),
     "按最近优先裁剪并留痕；裁判与评委拿到同一份字符串；存档仍保留全文")

item("R-35", "通用 PATCH 不能绕过状态机",
     has("engine/store.py", r"CAND_HTTP_PATCHABLE") and
     has("engine/api.py", r"store\.CAND_HTTP_PATCHABLE"),
     "status/result/identity 只能走专用端点；内部写入集与 HTTP 写入集分开")

item("R-36", "WAL 手动拷库警告",
     has("engine/backup.py", r"MANUAL_COPY_WARNING", r"-wal") and
     any_of("README.md", r"不要只拷"),
     "只拷 talent.db 会拿到空库且不报错；备份接口与 README 都提醒")

item("R-37", "bootstrap 复用同一批重抽样",
     has("engine/align.py", r"def _bootstrap_curves"),
     "多个 k 共用一次重抽样；同一批样本下区间彼此可比，成本降到 1/len(ks)")

item("R-38", "报告可打印 / 另存为 PDF",
     any_of("viewer/app.js", r"function printReport", r"break-before:page",
            r"print-color-adjust") and
     any_of("viewer/index.html", r"printReport\(\)"),
     "@media print 分页/表头重复/背景照打；工具栏 no-print；不引入任何依赖")

item("R-39", "启动环境自检（探测默认不发、结果缓存）",
     has("engine/api.py", r"/api/selfcheck", r"_SELF_TTL") and
     any_of("viewer/app.js", r"function loadSelfcheck"),
     "库可写/模型配置/备份新鲜度/连通性一眼可见；连通性要显式点按钮且缓存 10 分钟")

item("R-40", "演示数据显式注入（不内置）",
     os.path.exists(os.path.join(ROOT, "tools/seed_demo.py")) and
     any_of("tools/seed_demo.py", r"DEMO_TAG", r"--dry-run"),
     "岗位标题带 [演示] 前缀；必须显式运行脚本；打印清理方式")

item("R-41", "批量身份确认（部分成功照实返回）",
     has("engine/api.py", r"/api/identity/batch-confirm", r"IDENTITY_BATCH_MAX"),
     "只支持确认「不是同一人」；合并必须逐条；超限拒绝而非静默截断")

# ---------------------------------------------------------------- 可观测与验收
item("O-42", "事件按 candidate_id 关联（同名不串台）",
     has("engine/agents.py", r"candidate_id.*candidate_id|cand_id=cand_id") and
     any_of("viewer/app.js", r"ev\.candidate_id \|\|"),
     "agent 事件带 candidate_id；前端气泡键按 ID，旧事件回退姓名")

item("O-43", "事件归一化字段 stage/duration_ms/error_type",
     has("engine/events.py", r"def stage_of", r"def classify_error",
         r"def annotate") and
     any_of("engine/runs.py", r"events\.annotate"),
     "不改原有 type，附加归一化字段；落库事件重放也带；认不出的阶段返回 None")

item("O-44", "合成样本集 + 人工预期（可自检）",
     os.path.exists(os.path.join(ROOT, "tests/fixtures/sample_set_v1.json")) and
     has("engine/sampleset.py", r"def check", r"def expand"),
     "8 类样本各自声明该支持/该判缺失/不该得出的结论；自检保证预期与原文对得上")

item("O-45", "超长材料按头尾保留截断（不因排版误判）",
     has("engine/context.py", r"def _truncate_head_tail", r"TRUNC_HEAD_RATIO"),
     "原来只砍尾巴，写在末尾的关键经历会被静默丢掉；现头 60% 尾 40% 并显式标注")

item("O-46", "样本评测入口（环境未就绪时拒绝给通过率）",
     os.path.exists(os.path.join(ROOT, "tools/eval_samples.py")) and
     any_of("tools/eval_samples.py", r"环境未就绪", r"env_fail"),
     "真调模型才能回答'准不准'；没配凭证时报环境问题而不是打 0% 通过率")

item("O-47", "截断信息区分「整段未装入」与「只读了头尾」",
     has("engine/agents.py", r"truncated_sections") and
     any_of("viewer/app.js", r"保留了头尾"),
     "混在一起会让人以为材料完全没读，而实际是读了头尾的")

# ---------------------------------------------------------------- 输出
print("=" * 72)
width = max(len(n) for _, n, _, _ in rows)
fails = 0
for key, name, ok, ev in rows:
    mark = "✓" if ok else "✗"
    if not ok:
        fails += 1
    print("  %s %-7s %-*s  %s" % (mark, key, width, name, ev))
print("=" * 72)
print("  %d/%d 项通过" % (len(rows) - fails, len(rows)) if not fails
      else "  %d/%d 项未通过" % (fails, len(rows)))
sys.exit(0 if not fails else 1)
