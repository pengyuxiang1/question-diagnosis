# question-diagnosis

> 一个「先交证据、再下结论」的问题定位 skill —— 用状态文件、引导脚本和门禁 hook，把"排查纪律"从提示词里搬进代码。

排查线上问题时，AI 常犯一个毛病：**递给你一个很像答案的答案**。措辞笃定、逻辑自洽，但那个答案是从"可能是什么"里推出来的，不是从证据里读出来的。

更麻烦的是：你明明把排查规矩写进了 skill，任务一长它照样忘——因为**写在提示词里的规矩，会被上下文稀释**。

这个 skill 治的就是这个毛病。

## 它怎么治：三件套

| 部件 | 文件 | 作用 |
|------|------|------|
| **状态文件** | `templates/task.md` → `tasks/*/task.md` | 每条假设强制写「验证方法 + 预期证据」，判定标准**事先写死、不许事后补** |
| **引导脚本** | `scripts/flow_guide.py` | 每个动作前现场算：当前阶段 / 下一步 / 怎么做 / **证据缺口清单** |
| **门禁 hook** | `hooks/diagnosis-gate.js` | 定位阶段偷改代码 → 拦；结论证据无出处 / 假设未收敛就下结论 → 提醒或拦 |

三者缺一不可：只有状态文件是纯记录；只有引导脚本 AI 会忘记调；只有 hook 没有状态可判。

## 安装

```bash
# 1. 放进 skills 目录（CodeBuddy / Claude Code / 其他 Agent 按自己的路径）
cp -r question-diagnosis ~/.codebuddy/skills/

# 2. 装门禁 hook（可选但推荐）——把 hooks/diagnosis-gate.js 复制到项目的 hooks 目录，
#    并按 SKILL.md「门禁 hook：怎么装」一节注册到 settings.json
```

## 快速开始

```bash
# 开一个新排查（从模板复制状态文件）
cp templates/task.md tasks/$(date +%F)-your-issue/task.md   # 先建目录

# 每个动作前先跑引导：告诉你当前阶段 / 下一步 / 缺什么证据
python3 scripts/flow_guide.py tasks/2026-01-01-your-issue/task.md

# 下结论前自检：退出码 1 = 有证据缺口，不许给根因
python3 scripts/flow_guide.py tasks/2026-01-01-your-issue/task.md --check
```

## 排查流程（协议全文见 SKILL.md）

```
Step 0  收事实（禁止写根因猜想）
Step 1  列假设（全部候选，每条能被证实/证伪）
Step 2  写 task.md（验证方法 + 预期证据，判定标准先写死）
Step 3  逐条验证（只信日志原文 / DB 值 / 代码行，证据带出处）
Step 4  复盘（未定位 → 带新证据开下一轮；禁止无新证据重复猜测）
Step 5  出结论（根因链每跳带证据 + 排除项清单）
Step 6  归档（会复现 → references/，同步更新 knowledge-map）
```

## 目录结构

```
question-diagnosis/
├── SKILL.md                     # 主流程（协议 + 门禁 + 设计原则）
├── scripts/flow_guide.py        # 引导脚本（零依赖、只读）
├── templates/task.md            # 状态文件模板（顶部钉引导指令）
├── hooks/diagnosis-gate.js      # 门禁 hook（PreToolUse + Stop）
├── tasks/                       # 你的排查工作区（每个问题一个目录）
└── references/
    ├── tool-map.md              # 骨架：现象 → 工具（按你的环境填）
    ├── knowledge-map.md         # 骨架：现象 → 历史案例索引
    └── {domain}-{issue}.md      # 归档的案例（越用越厚）
```

## 设计原则

1. **记忆在磁盘上，不在上下文里**——状态文件落盘，压缩、换会话、换模型都不怕
2. **判定标准必须能机器校验**——"证据充分"是虚的，"证据段含 `文件:行号`"是实的
3. **约束落在产物上**——不靠自觉，靠程序对 task.md 逐行检查
4. **fail-open**——门禁自身出错时放行，绝不阻塞正事

## 让它变成你的 skill

`references/` 下的三个 map 和领域手册都是骨架——**填得越厚，排查越快**。

这也是它"越用越好"的部分：每排查完一个会复现的问题，归档一个案例、补一行索引，下次同类问题直接命中。

> ⚠️ **升级备份**：`references/`（你的案例库）和 `tasks/`（进行中的排查）是你的积累，不在上游仓库里——`git pull` 更新本 skill 前先备份这两个目录。知识与 skill 完全解耦的做法见 SKILL.md「运行时知识沉淀」一节。

## 姊妹项目

[**skillforge**](https://github.com/pengyuxiang1/skillforge) —— 生产这类 skill 的生产器。本 skill 就是被它改造出来的实例（有状态机、有引导、有门禁的"超级 skill"）。

## License

MIT

---

> Note for English readers: the skill content is written in Chinese. The workflow itself is language-agnostic — translate `SKILL.md` and the templates if you want to use it in English.
