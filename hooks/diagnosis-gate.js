#!/usr/bin/env node
/**
 * diagnosis-gate.js — 问题定位流程的阶段门禁 hook
 *
 * 设计要点（回答"hook 怎么按阶段动态生效"）：
 *   hook 是**常挂**的，不按阶段插拔。事件发生时脚本一定会被调用，
 *   由脚本内部读 task.md 的状态决定「这次拦不拦」。
 *
 *   两层过滤：
 *     第一层（配置层 matcher）：只对 Edit|Write 这类写文件的工具触发
 *     第二层（脚本内判断）：读 task.md，规划阶段才拦，修复阶段放行
 *
 * 覆盖两类问题：
 *   【问题1】长任务后忘记调用 flow_guide.py → Stop 时检查并提醒
 *   【问题2】规划阶段偷写业务代码 → PreToolUse 时拦截
 *
 * 安全原则：任何异常都放行（fail-open），绝不因为 hook 自身出错而阻塞 AI。
 *
 * 环境变量：
 *   DIAG_GATE_OFF=1     完全关闭本 hook
 *   DIAG_GATE_ENFORCE=1 Stop 阶段缺引导时改为阻断（默认仅提醒）
 */

'use strict';

const fs = require('fs');
const path = require('path');

const MAX_STDIN = 1024 * 1024;

// 规划阶段（不允许改业务代码）
const PLANNING_STAGES = new Set(['collect', 'hypothesize', 'verify', 'conclude']);

// 允许写入的路径（定位期间的留痕区，不算改业务代码）
const ALLOWED_PATH_PATTERNS = [
  /\.codebuddy\/(skills\/question-diagnosis\/(tasks|references|scripts)|aidev|hooks)\//,
  /(^|\/)tasks\/\d{4}-\d{2}-\d{2}-[^/]+\//,
  /(^|\/)\.codebuddy\/settings(\.local)?\.json$/,
];

// 明确属于"改业务代码"的扩展名（规划阶段拦截目标）
const CODE_EXT = /\.(go|java|py|ts|tsx|js|jsx|vue|c|cpp|h|hpp|rs|rb|php|cs|kt|swift|sql|proto|yaml|yml|toml|json|sh|gradle|xml)$/i;

// 写文件类工具名 —— 以 codebuddy bundle 的权威工具表为准（v2.148.0 实测）
// 权威来源：dist/codebuddy.js 内 ["Edit","MultiEdit","Glob","Grep","Bash",...] 工具名清单
//   及 n3=new Set(["Read","Grep","Glob","Write","WebFetch","WebSearch","Edit","Skill","Bash"])
//   及 class NotebookEdit{ this.name="NotebookEdit" }
// 注意：write_to_file / replace_in_file 是 CodeBuddy **IDE** 的工具名，CLI bundle 中不存在
//       （已用 grep -c 验证为 0）。此处仅为兼容其它运行环境而保留。
const WRITE_TOOLS = /^(Edit|MultiEdit|Write|NotebookEdit|apply_patch|write_to_file|replace_in_file)$/i;

function readStdin() {
  try {
    return fs.readFileSync(0, 'utf8');
  } catch {
    return '';
  }
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** 找到 question-diagnosis 的根目录（skill 目录） */
function findSkillDir(cwd) {
  const candidates = [
    path.join(cwd, '.codebuddy', 'skills', 'question-diagnosis'),
    path.join(cwd, '.claude', 'skills', 'question-diagnosis'),
  ];
  return candidates.find((p) => fs.existsSync(p)) || null;
}

/** 列出进行中的排查 task.md */
function listActiveTasks(skillDir) {
  const tasksDir = path.join(skillDir, 'tasks');
  if (!fs.existsSync(tasksDir)) return [];
  const out = [];
  for (const name of fs.readdirSync(tasksDir)) {
    const tf = path.join(tasksDir, name, 'task.md');
    if (fs.existsSync(tf)) out.push(tf);
  }
  return out;
}

/** 解析 task.md 的基本状态（与 flow_guide.py 同源的轻量实现） */
function parseTask(file) {
  let raw;
  try {
    raw = fs.readFileSync(file, 'utf8');
  } catch {
    return null;
  }
  const lines = raw.replace(/\r\n/g, '\n').split('\n');

  // 事实节是否有实质内容
  let hasFacts = false;
  let inFacts = false;
  let hasConclusion = false;
  for (const line of lines) {
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const t = h[2].trim();
      inFacts = /^(问题事实|Step\s*0|现象)/i.test(t);
      if (/^(Step\s*[45]|结论|根因|修复方案)/i.test(t)) hasConclusion = true;
      continue;
    }
    if (inFacts) {
      const s = line.trim();
      if (!s || s.startsWith('>')) continue;
      if (/^-\s*\*\*[^*]+\*\*\s*(?:[（(][^）)]*[）)])?\s*[:：]\s*$/.test(s)) continue;
      hasFacts = true;
    }
  }

  // 假设统计
  const byId = new Map();
  for (const line of lines) {
    const m = line.match(/^\|\s*(H\d+[a-z]?)\s*\|/i);
    if (m) {
      const id = m[1].toUpperCase();
      byId.set(id, { status: detectStatus(line) });
      continue;
    }
    const hm = line.match(/^#{2,6}\s*(H\d+[a-z]?)\b(.*)$/i);
    if (hm) {
      const id = hm[1].toUpperCase();
      byId.set(id, { status: detectStatus(line) });
    }
    const sm = line.match(/^[-*]\s*状态\s*[:：]\s*(.+)$/);
    if (sm && byId.size > 0) {
      const lastKey = Array.from(byId.keys()).pop();
      byId.set(lastKey, { status: detectStatus(sm[1]) });
    }
  }

  const counts = { confirmed: 0, refuted: 0, partial: 0, pending: 0 };
  for (const v of byId.values()) counts[v.status] = (counts[v.status] || 0) + 1;

  let stage;
  if (!hasFacts) stage = 'collect';
  else if (byId.size === 0) stage = 'hypothesize';
  else if (counts.pending + counts.partial > 0) stage = 'verify';
  else if (!hasConclusion) stage = 'conclude';
  else stage = 'done';

  return { stage, counts, total: byId.size, hasFacts };
}

function detectStatus(text) {
  if (/已证实|🟢|✅/.test(text)) return 'confirmed';
  if (/已排除|已证伪|❌|🔴/.test(text)) return 'refuted';
  if (/部分|🟡|⏳|待补|未直接证实|证据不足/.test(text)) return 'partial';
  return 'pending';
}

function isPlanning(stage) {
  return PLANNING_STAGES.has(stage);
}

/** 该路径是否允许在规划阶段写入 */
function isAllowedPath(target) {
  if (!target) return true;
  const norm = target.replace(/\\/g, '/');
  return ALLOWED_PATH_PATTERNS.some((re) => re.test(norm));
}

function emit(obj) {
  process.stdout.write(JSON.stringify(obj));
}

// ---------------------------------------------------------------------------
// PreToolUse：规划阶段禁止改业务代码
// ---------------------------------------------------------------------------
function handlePreToolUse(hook, cwd, skillDir) {
  const tool = hook.tool_name || hook.toolName || '';
  if (!WRITE_TOOLS.test(tool)) return null; // 非写文件工具，放行

  const ti = hook.tool_input || hook.toolInput || {};
  const target =
    ti.file_path || ti.filePath || ti.path || ti.target_file || ti.notebook_path || '';
  if (isAllowedPath(target)) return null; // 留痕区，放行

  const active = listActiveTasks(skillDir)
    .map(parseTask)
    .filter((t) => t && isPlanning(t.stage));
  if (active.length === 0) return null; // 没有进行中的定位，放行

  const stages = active.map((t) => t.stage).join(', ');
  const isCode = CODE_EXT.test(target);

  // 官方 API（Hooks 配置参考 · PreToolUse 决策控制）：
  //   hookSpecificOutput.permissionDecision = "allow" | "deny" | "ask"
  // 注意：顶层 decision:"block" 已废弃，PreToolUse 必须走 hookSpecificOutput。
  const reason =
    `[诊断门禁] 当前存在进行中的问题定位（阶段：${stages}），定位期间禁止修改业务代码。\n` +
    `被拦下的写入：${path.basename(target)}${isCode ? '（代码文件）' : ''}\n` +
    `定稿前应先跑：python3 .codebuddy/skills/question-diagnosis/scripts/flow_guide.py <task.md> --check\n` +
    `如需修复代码，请先完成定位并更新 task.md 状态；确需放行请设置 DIAG_GATE_OFF=1。`;

  return {
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: reason,
    },
  };
}

// ---------------------------------------------------------------------------
// Stop：长任务后引导丢失 → 提醒/阻断
// ---------------------------------------------------------------------------
function handleStop(hook, cwd, skillDir) {
  const active = listActiveTasks(skillDir);
  if (active.length === 0) return null;

  // A) 检查结论合规性：有结论但缺证据 → 必须提醒
  const failures = [];
  const reminders = [];
  for (const f of active) {
    let raw;
    try {
      raw = fs.readFileSync(f, 'utf8');
    } catch {
      continue;
    }
    const name = path.basename(path.dirname(f));

    // ① 正文里以标题形式书写的假设：### H1（已证实 ✅）：xxx + 正文即证据
    const blocks = raw.split(/\n(?=#{2,6}\s*\S)/i);
    for (const b of blocks) {
      const title = (b.match(/^#{2,6}\s*(H\d+[a-z]?)\b[^\n]*/i) || [])[0];
      if (!title) continue;
      if (!/已证实|🟢|✅|已排除|🔴|❌/.test(title)) continue;
      const hid = (title.match(/H\d+[a-z]?/i) || ['?'])[0].toUpperCase();
      const hasEvidence = /[\w/\-.]+\.\w{1,6}:\d+|\[\s*(日志平台|DB|数据库|日志|trace|索引集)|trace_id\s*[:=]|index\s*=\s*\d+|```/.test(b);
      if (!hasEvidence) failures.push(`${name} → ${hid} 标已证实/已排除但证据无可复查出处`);
    }

    // ② 表格形式书写的假设：| H1 | 表述 | 验证方法 | 预期证据 |（行内标记已证实/已排除）
    //    表格行没有正文证据段，因此「行内标已证实/已排除」= 无出处证据 → 必须提醒
    for (const line of raw.split('\n')) {
      const m = line.match(/^\|\s*(H\d+[a-z]?)\s*\|/i);
      if (!m) continue;
      const cells = line.split('|').map((s) => s.trim());
      if (cells.length < 2 || /^[-:\s]+$/.test(cells[1] || '')) continue; // 表头/分隔行
      const hid = m[1].toUpperCase();
      const hasInlineEvidence = /[\w/\-.]+\.\w{1,6}:\d+|trace_id\s*[:=]|index\s*=\s*\d+|\[\s*(日志平台|DB|日志|trace)/.test(line);
      if (/已证实|🟢|✅|已排除|🔴/.test(line) && !hasInlineEvidence) {
        failures.push(`${name} → ${hid} 表格行内标已证实/已排除，但无出处证据（表格无正文证据段，需在 Step 2 补标题式验证记录）`);
      }
    }

    // ③ 有结论但无 🟢
    if (/(^|\n)#{2,4}\s*(Step\s*[45]|结论|根因)/i.test(raw) && !/🟢|已证实/.test(raw)) {
      failures.push(`${name} → 已写结论，但没有任何 🟢 已证实的假设支撑`);
    }

    // ④ 阶段未收敛：仍有未验证/部分证实的假设，但已写结论
    const st = parseTask(f);
    if (st && (st.counts.pending + st.counts.partial) > 0) {
      const head = /(^|\n)#{2,4}\s*(Step\s*[45]|结论|根因)/i.test(raw);
      if (head) {
        const open = st.counts.pending + st.counts.partial;
        failures.push(`${name} → 仍有 ${open} 条假设未收敛（🟡），却已写结论：不得把未证实假设当根因（需标「待补证」或继续验证）`);
      } else {
        reminders.push(`${name} → 仍有 ${open} 条假设未收敛，下一步：${st.stage}`);
      }
    }
  }

  if (failures.length === 0 && reminders.length === 0) return null;

  const parts = [];
  if (failures.length > 0) {
    parts.push(
      `[诊断门禁] 检测到 ${failures.length} 处证据缺口：\n` +
        failures.map((s) => `  ✗ ${s}`).join('\n') +
        `\n请跑 --check 自检并补齐证据后再下结论：` +
        `python3 .codebuddy/skills/question-diagnosis/scripts/flow_guide.py <task.md> --check`
    );
  }
  if (reminders.length > 0) {
    parts.push(
      `[诊断提醒] 排查仍在进行中：\n` +
        reminders.map((s) => `  · ${s}`).join('\n') +
        `\n下一步引导：python3 .codebuddy/skills/question-diagnosis/scripts/flow_guide.py <task.md>`
    );
  }
  const body = parts.join('\n\n');

  // Stop 决策 API（官方文档）：
  //   continue:false = 阻止停止，让 Agent 继续工作，reason 注入对话历史
  //   systemMessage = 仅显示给用户，不传给 Agent
  // 默认只在 UI 提醒用户（systemMessage），不打断对话；ENFORCE 时才阻止停止。
  if (failures.length > 0 && process.env.DIAG_GATE_ENFORCE === '1') {
    return { continue: false, reason: body };
  }
  return { systemMessage: body };
}

// ---------------------------------------------------------------------------
function main() {
  if (process.env.DIAG_GATE_OFF === '1') {
    emit({});
    return;
  }

  const input = readStdin().slice(0, MAX_STDIN);
  const hook = safeJson(input) || {};
  const event = hook.hook_event_name || hook.hookEventName || process.env.CODEBUDDY_HOOK_EVENT || '';
  const cwd = hook.cwd || process.cwd();

  const skillDir = findSkillDir(cwd);
  if (!skillDir) {
    emit({});
    return;
  }

  let result = null;
  try {
    if (/PreToolUse/i.test(event)) result = handlePreToolUse(hook, cwd, skillDir);
    else if (/^Stop$/i.test(event)) result = handleStop(hook, cwd, skillDir);
  } catch {
    result = null; // fail-open
  }

  emit(result || {});
}

main();
