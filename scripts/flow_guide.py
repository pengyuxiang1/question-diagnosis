#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flow_guide.py — 问题定位引导器 / 证据门禁（question-diagnosis skill）

设计目标（对标 openspec 的 `openspec instructions --json` 机制）：
  1. 读 task.md 的**当前状态**，推断「阶段 / 进度 / 下一步该做什么」
  2. 输出**结构化引导**：下一步动作 + 怎么做 + 判定标准 + 缺口清单
  3. `--check` 模式做**证据门禁**：证据不合规时退出码 1（可被 hook 调用拦截）

为什么需要它：
  纯文字的 SKILL.md 协议是软约束，长任务后 AI 会忘记「要验证、要证据」。
  把引导做成「AI 必须调用、且返回值直接告诉它下一步」的 CLI，等于把
  提示词从「上下文里会沉底的一段话」变成「每次都要现场拉取的状态」。

用法:
    python3 flow_guide.py <task.md>              # 人类可读引导
    python3 flow_guide.py <task.md> --json       # 结构化引导（AI 读）
    python3 flow_guide.py <task.md> --check      # 门禁校验（退出码 0/1）
    python3 flow_guide.py --list                 # 列出所有进行中的排查

退出码:
    0 = 引导正常 / 门禁通过
    1 = 门禁不通过（存在必须补齐的证据缺口）
    2 = 用法错误 / 文件不存在
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any

# ---------------------------------------------------------------------------
# 常量：状态定义与证据判定
# ---------------------------------------------------------------------------

STATUS_CONFIRMED = "confirmed"   # 🟢 已证实
STATUS_REFUTED = "refuted"       # 🔴 已排除
STATUS_PARTIAL = "partial"       # 🟡 部分证实 / 待补证
STATUS_PENDING = "pending"       # 🟡 未验证

STATUS_LABEL = {
    STATUS_CONFIRMED: "🟢 已证实",
    STATUS_REFUTED: "🔴 已排除",
    STATUS_PARTIAL: "🟡 部分证实（待补证）",
    STATUS_PENDING: "🟡 未验证",
}

# 阶段定义（这就是最小状态机）
STAGE_COLLECT = "collect"           # 搜集事实
STAGE_HYPOTHESIZE = "hypothesize"   # 列假设
STAGE_VERIFY = "verify"             # 逐条验证
STAGE_CONCLUDE = "conclude"         # 出结论
STAGE_DONE = "done"                 # 已归档

STAGE_LABEL = {
    STAGE_COLLECT: "Step 0 搜集问题（只收事实）",
    STAGE_HYPOTHESIZE: "Step 1 列假设清单",
    STAGE_VERIFY: "Step 3 逐条验证假设",
    STAGE_CONCLUDE: "Step 4/5 汇总证据链、输出结论",
    STAGE_DONE: "已定位完成",
}

# 「证据可复查」的判定标记：命中任一即认为证据带出处
EVIDENCE_MARKERS = [
    (r"[\w/\-\.]+\.\w{1,6}:\d+", "文件:行号"),
    (r"\[\s*(日志平台|DB|数据库|日志|trace|索引集|配置平台)", "出处标注（[日志平台 xxx] 等）"),
    (r"trace_id\s*[:=]", "trace_id"),
    (r"index\s*=\s*\d+", "索引集 index=xxx"),
    (r"```", "代码块原文"),
    (r"[a-z][a-z0-9_]{5,}(_trace|_log|_perf)", "索引集名"),
]

# 结论节标题
CONCLUSION_HEADING = re.compile(r"^#{2,4}\s*(Step\s*4|Step\s*5|结论|根因|修复方案)", re.I)
FACTS_HEADING = re.compile(r"^#{2,4}\s*(问题事实|Step\s*0|现象)", re.I)
HYPOTHESIS_TABLE_HEAD = re.compile(r"^#{2,4}\s*(Step\s*1|假设清单|假设)", re.I)
HYPOTHESIS_ROW = re.compile(r"^\|\s*(H\d+[a-z]?)\s*\|(.+)$", re.I)
HYPOTHESIS_HEADING = re.compile(r"^(#{2,4})\s*(H\d+[a-z]?)\b(.*)$", re.I)
ANY_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Hypothesis:
    id: str
    statement: str = ""
    status: str = STATUS_PENDING
    verify_method: str = ""
    expected: str = ""
    evidence: str = ""
    evidence_issues: List[str] = field(default_factory=list)
    line: int = 0

    @property
    def label(self) -> str:
        return STATUS_LABEL.get(self.status, self.status)


@dataclass
class TaskState:
    path: str
    title: str = ""
    has_facts: bool = False
    facts_body: str = ""
    hypotheses: List[Hypothesis] = field(default_factory=list)
    has_conclusion: bool = False
    conclusion_body: str = ""
    has_rounds: bool = False
    lines_total: int = 0

    @property
    def counts(self) -> Dict[str, int]:
        c = {STATUS_CONFIRMED: 0, STATUS_REFUTED: 0, STATUS_PARTIAL: 0, STATUS_PENDING: 0}
        for h in self.hypotheses:
            c[h.status] = c.get(h.status, 0) + 1
        return c


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def detect_status(text: str) -> str:
    """从文本片段判断假设状态。顺序敏感：先强信号，再弱信号。

    注意：不要用裸「证伪」做模式——「可证伪的表述」是方法论术语，会误判成 🔴。
    """
    if not text:
        return STATUS_PENDING
    if re.search(r"已证实|🟢|✅", text):
        return STATUS_CONFIRMED
    if re.search(r"已排除|已证伪|❌|🔴", text):
        return STATUS_REFUTED
    if re.search(r"部分|🟡|⏳|待补|未直接证实|证据不足", text):
        return STATUS_PARTIAL
    return STATUS_PENDING


def clean_statement(tail: str) -> str:
    """从标题尾部提取假设表述：剥离括号状态标注与「假设：」前缀。"""
    s = re.sub(r"^[（(][^）)]*[）)]\s*[:：]?", "", tail).strip()
    s = re.sub(r"^假设\s*[:：]\s*", "", s).strip()
    return s


def split_sections(lines: List[str]) -> List[Dict[str, Any]]:
    """把文档按标题切成节：[{level, title, start, end}]"""
    sections = []
    for idx, line in enumerate(lines):
        m = ANY_HEADING.match(line)
        if m:
            sections.append({
                "level": len(m.group(1)),
                "title": m.group(2).strip(),
                "start": idx,
                "end": len(lines),
            })
    for i in range(len(sections) - 1):
        sections[i]["end"] = sections[i + 1]["start"]
    return sections


def body_of(lines: List[str], sec: Dict[str, Any]) -> str:
    return "\n".join(lines[sec["start"] + 1: sec["end"]]).strip()


PLACEHOLDER_LINE = re.compile(r"^-\s*\*\*[^*]+\*\*\s*(?:[（(][^）)]*[）)])?\s*[:：]\s*$")


def has_meaningful_content(body: str) -> bool:
    """判断一节是否有实质内容（排除空占位行与说明引用块）。

    模板刚复制时整节都是 `- **现象**：` 这类空值行，不应被当成「已完成」。
    """
    for line in body.split("\n"):
        s = line.strip()
        if not s or s.startswith(">") or s.startswith("#"):
            continue
        if PLACEHOLDER_LINE.match(s):
            continue
        if re.match(r"^[-*]\s*$", s):
            continue
        return True
    return False


def evidence_issues_for(evidence: str, status: str) -> List[str]:
    """检查证据段落的可复查性。只对已证实/已排除的假设做硬判定。"""
    issues = []
    if status not in (STATUS_CONFIRMED, STATUS_REFUTED):
        return issues
    if not evidence.strip():
        issues.append("缺少「实际证据」段落")
        return issues
    hits = [label for pat, label in EVIDENCE_MARKERS if re.search(pat, evidence)]
    if not hits:
        issues.append(
            "证据段落没有可复查出处（需含 文件:行号 / [索引集+时间窗] / trace_id / 代码块 之一）"
        )
    return issues


def parse_task(path: str) -> TaskState:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    lines = raw.replace("\r\n", "\n").split("\n")
    st = TaskState(path=path, lines_total=len(lines))

    # 标题
    for line in lines:
        if line.startswith("# "):
            st.title = line[2:].strip()
            break
    if not st.title:
        st.title = os.path.basename(os.path.dirname(path)) or os.path.basename(path)

    sections = split_sections(lines)

    # 事实节
    for sec in sections:
        if FACTS_HEADING.match("#" * sec["level"] + " " + sec["title"]):
            body = body_of(lines, sec)
            if has_meaningful_content(body):
                st.has_facts = True
                st.facts_body = body
            break

    # 结论节
    for sec in sections:
        if CONCLUSION_HEADING.match("#" * sec["level"] + " " + sec["title"]):
            body = body_of(lines, sec)
            if body:
                st.has_conclusion = True
                st.conclusion_body = body
            break

    # 轮次标记（Round N / 第 N 轮）
    if re.search(r"^#{2,4}\s*(Round\s*\d|第\s*\d+\s*轮)", raw, re.I | re.M):
        st.has_rounds = True

    by_id: Dict[str, Hypothesis] = {}

    # ① 表格形态：| H1 | 表述 | 验证方法 | 预期证据 |
    for idx, line in enumerate(lines):
        m = HYPOTHESIS_ROW.match(line.strip())
        if not m:
            continue
        hid = m.group(1).upper()
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # cells: [H1, 表述, 验证方法, 预期证据, ...]
        if len(cells) < 2 or not cells[1] or set(cells[1]) <= set("-: "):
            continue  # 表头或分隔行
        h = Hypothesis(
            id=hid,
            statement=cells[1],
            verify_method=cells[2] if len(cells) > 2 else "",
            expected=cells[3] if len(cells) > 3 else "",
            line=idx + 1,
        )
        h.status = detect_status(line)
        by_id[hid] = h

    # ② 标题形态：### H1（已证实 ✅）：xxx   + 其正文作为证据
    for sec in sections:
        m = HYPOTHESIS_HEADING.match("#" * sec["level"] + " " + sec["title"])
        if not m:
            continue
        hid = m.group(2).upper()
        tail = m.group(3).strip()
        body = body_of(lines, sec)
        h = by_id.get(hid)
        if h is None:
            h = Hypothesis(id=hid, statement=clean_statement(tail))
            by_id[hid] = h
            h.line = sec["start"] + 1
        # 状态优先级：显式 `- 状态：` 字段 > 标题括号标注 > 表格行内标记
        m_status = re.search(r"^[-*]\s*状态\s*[:：]\s*(.+)$", body, re.M)
        if m_status:
            h.status = detect_status(m_status.group(1))
        else:
            st_from_head = detect_status(sec["title"])
            if st_from_head != STATUS_PENDING or h.status == STATUS_PENDING:
                h.status = st_from_head
        if body:
            h.evidence = body
            # 标题形态下解析字段行（模板约定的 `- 验证方法：` / `- 预期证据：`）
            m_verify = re.search(r"^[-*]\s*\**验证方法\**\s*[:：]\s*(.+)$", body, re.M)
            if m_verify and not h.verify_method:
                h.verify_method = m_verify.group(1).strip()
            m_expected = re.search(r"^[-*]\s*\**预期证据\**\s*[:：]\s*(.+)$", body, re.M)
            if m_expected and not h.expected:
                h.expected = m_expected.group(1).strip()
        if not h.statement:
            h.statement = clean_statement(tail)

    # 排序：按 H 编号数字序
    def sort_key(h: Hypothesis):
        m = re.match(r"H(\d+)", h.id)
        return (int(m.group(1)) if m else 999, h.id)

    st.hypotheses = sorted(by_id.values(), key=sort_key)

    # 逐条计算证据缺口
    for h in st.hypotheses:
        h.evidence_issues = evidence_issues_for(h.evidence, h.status)
        if h.status in (STATUS_CONFIRMED, STATUS_REFUTED):
            if not h.verify_method:
                h.evidence_issues.append("缺少「验证方法」（未先写死判定标准）")
            if not h.expected:
                h.evidence_issues.append("缺少「预期证据」（未先写死证实/证伪标准）")

    return st


# ---------------------------------------------------------------------------
# 状态推断 & 引导生成
# ---------------------------------------------------------------------------

def infer_stage(st: TaskState) -> str:
    if not st.has_facts:
        return STAGE_COLLECT
    if not st.hypotheses:
        return STAGE_HYPOTHESIZE
    c = st.counts
    if c[STATUS_PENDING] + c[STATUS_PARTIAL] > 0:
        return STAGE_VERIFY
    if not st.has_conclusion:
        return STAGE_CONCLUDE
    return STAGE_DONE


def build_guidance(st: TaskState) -> Dict[str, Any]:
    stage = infer_stage(st)
    c = st.counts
    total = len(st.hypotheses)

    # 下一步动作
    next_action = ""
    how = ""
    if stage == STAGE_COLLECT:
        next_action = "补齐「问题事实」节（只写事实，禁止写根因猜想）"
        how = ("写入 现象 / 时间窗 / 环境 / 用户原话 / 相关 ID（trace、conversation、订单号）。"
               "本步不写任何原因推断。")
    elif stage == STAGE_HYPOTHESIZE:
        next_action = "列出全部候选假设，每条写死「验证方法 + 预期证据」"
        how = ("每条假设必须可证伪，并落到：查哪份日志/哪个索引集/哪段代码。"
               "禁止写「可能是网络问题」这类无法落到证据的空泛假设。")
    elif stage == STAGE_VERIFY:
        target = next((h for h in st.hypotheses if h.status in (STATUS_PENDING, STATUS_PARTIAL)), None)
        if target:
            next_action = "验证 %s：%s" % (target.id, target.statement[:60])
            how = target.verify_method or "（该假设未写验证方法，先补上：查哪份日志/哪个索引集/哪段代码）"
            if target.status == STATUS_PARTIAL:
                how += "\n  ⚠️ 该假设是「部分证实」，需要补上缺的那条证据才能升级为 🟢"
        else:
            next_action = "所有假设已判定，进入结论汇总"
    elif stage == STAGE_CONCLUDE:
        next_action = "汇总证据链，输出结论（Step 5）"
        how = ("根因链每一跳都要有证据；写排除项清单（哪些假设被证伪 + 依据）；"
               "证据链有缺口的结论必须标「待补证」，不得当根因。")
    else:
        next_action = "已定位完成 —— 判断是否归档为 references/{domain}-{issue}.md"
        how = "会复现/可能再遇 → 提炼案例并更新 knowledge-map.md；一次性 → 不归档。"

    # 缺口清单
    gaps = []
    for h in st.hypotheses:
        if "{" in h.statement and "}" in h.statement:
            gaps.append("%s：假设表述仍是模板占位符，未填写" % h.id)
        for issue in h.evidence_issues:
            gaps.append("%s（%s）：%s" % (h.id, h.label, issue))

    # 未验证假设提示
    pending = [h.id for h in st.hypotheses if h.status in (STATUS_PENDING, STATUS_PARTIAL)]

    return {
        "task": st.title,
        "state_file": st.path,
        "stage": stage,
        "stage_label": STAGE_LABEL[stage],
        "progress": {
            "total": total,
            "confirmed": c[STATUS_CONFIRMED],
            "refuted": c[STATUS_REFUTED],
            "partial": c[STATUS_PARTIAL],
            "pending": c[STATUS_PENDING],
            "unresolved": pending,
        },
        "next": next_action,
        "how": how,
        "gaps": gaps,
        "has_conclusion": st.has_conclusion,
        "can_write_code": False,
        "reminder": (
            "① 标 🟢/🔴 前必须有证据（出处 + 原文 + 复查方式），[推断] 必须与证据分开标注；"
            "② 严禁把 [推断] 当结论写进回复；"
            "③ 未定位时基于**本轮新证据**开下一轮（旧假设标 🔴 + 写明证伪依据），"
            "禁止无新证据地重复猜测。"
        ),
    }


# ---------------------------------------------------------------------------
# 门禁校验（--check）
# ---------------------------------------------------------------------------

def run_check(st: TaskState) -> Dict[str, Any]:
    """只校验「已声明结论」的合规性，不要求排查必须完成。
    语义：当前状态下的产物是否经得起复查。"""
    failures = []
    warnings = []

    if not st.has_facts:
        failures.append("「问题事实」节为空 —— Step 0 未完成（事实是后续所有推理的地基）")

    for h in st.hypotheses:
        for issue in h.evidence_issues:
            if h.status in (STATUS_CONFIRMED, STATUS_REFUTED):
                failures.append("%s 标「%s」但 %s" % (h.id, h.label, issue))
            else:
                warnings.append("%s（%s）：%s" % (h.id, h.label, issue))

    # 结论必须建立在已证实的假设上
    if st.has_conclusion and st.counts[STATUS_CONFIRMED] == 0:
        failures.append("已写结论，但没有任何 🟢 已证实的假设支撑 —— 结论无证据基础")

    # 结论里出现的假设 ID 必须存在
    if st.has_conclusion:
        refs = set(re.findall(r"\bH\d+[a-z]?\b", st.conclusion_body, re.I))
        known = {h.id for h in st.hypotheses}
        unknown = {r.upper() for r in refs} - known
        if unknown and known:
            warnings.append("结论引用了不存在的假设 ID：%s" % ", ".join(sorted(unknown)))

    return {
        "pass": len(failures) == 0,
        "failures": failures,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def render_text(g: Dict[str, Any], check: Optional[Dict[str, Any]] = None) -> str:
    p = g["progress"]
    out = []
    out.append("=" * 66)
    out.append("📋 %s" % g["task"])
    out.append("=" * 66)
    out.append("")
    out.append("【当前阶段】%s" % g["stage_label"])
    out.append("【进度】假设 %d 条 —— 🟢%d / 🔴%d / 🟡%d（其中未解决：%s）" % (
        p["total"], p["confirmed"], p["refuted"], p["partial"] + p["pending"],
        ", ".join(p["unresolved"]) if p["unresolved"] else "无",
    ))
    out.append("")
    out.append("▶ 下一步：%s" % g["next"])
    for line in g["how"].split("\n"):
        out.append("   %s" % line)
    out.append("")

    if g["gaps"]:
        out.append("⚠️ 证据缺口（%d 处）：" % len(g["gaps"]))
        for gap in g["gaps"]:
            out.append("   - %s" % gap)
        out.append("")

    out.append("🔒 约束：can_write_code = false（定位期间禁止修改业务代码）")
    out.append("💡 提醒：%s" % g["reminder"])
    out.append("")

    if check is not None:
        out.append("-" * 66)
        if check["pass"]:
            out.append("✅ 门禁校验：通过")
        else:
            out.append("❌ 门禁校验：不通过（%d 项必须补齐）" % len(check["failures"]))
            for f in check["failures"]:
                out.append("   ✗ %s" % f)
        for w in check["warnings"]:
            out.append("   ⚠ %s" % w)
        out.append("")

    return "\n".join(out)


def list_tasks(base_dir: str) -> int:
    tasks_dir = os.path.join(base_dir, "tasks")
    if not os.path.isdir(tasks_dir):
        print("未找到 tasks 目录：%s" % tasks_dir)
        return 0
    found = []
    for name in sorted(os.listdir(tasks_dir)):
        d = os.path.join(tasks_dir, name)
        tf = os.path.join(d, "task.md")
        if os.path.isfile(tf):
            found.append(tf)
    if not found:
        print("（无进行中的排查）")
        return 0
    print("进行中的排查（%d）：" % len(found))
    for tf in found:
        try:
            st = parse_task(tf)
            g = build_guidance(st)
            print("  - %s" % tf)
            print("      阶段：%s | 进度：%d/%d 已判定" % (
                g["stage"], g["progress"]["confirmed"] + g["progress"]["refuted"], g["progress"]["total"]))
        except Exception as exc:  # noqa: BLE001
            print("  - %s（解析失败：%s）" % (tf, exc))
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="问题定位引导器 / 证据门禁（question-diagnosis）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("task", nargs="?", help="task.md 路径")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON（给 AI 读）")
    parser.add_argument("--check", action="store_true", help="门禁校验模式（退出码 0/1）")
    parser.add_argument("--list", action="store_true", help="列出所有进行中的排查")
    args = parser.parse_args(argv)

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if args.list:
        return list_tasks(base_dir)

    if not args.task:
        parser.print_help()
        return 2

    path = args.task
    if not os.path.isabs(path) and not os.path.isfile(path):
        cand = os.path.join(base_dir, path)
        if os.path.isfile(cand):
            path = cand
    if not os.path.isfile(path):
        sys.stderr.write("文件不存在：%s\n" % args.task)
        return 2

    st = parse_task(path)
    guidance = build_guidance(st)
    check_result = run_check(st) if args.check else None

    if args.json:
        payload = dict(guidance)
        if check_result is not None:
            payload["check"] = check_result
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(guidance, check_result))

    if check_result is not None and not check_result["pass"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
