# 问题定位库（tasks/）

> 进行中排查的工作区。每个问题一个子目录：`tasks/YYYY-MM-DD-{问题slug}/task.md`
> （**从 `templates/task.md` 复制开工**，顶部自带引导指令）。
> 排查流程见 `SKILL.md`「排查工作流协议（刨根问底版）」。

## 🔁 引导机制（不要凭记忆推进）

```bash
# 每个动作前先跑：读 task.md 算出当前阶段 + 下一步 + 证据缺口
python3 .codebuddy/skills/question-diagnosis/scripts/flow_guide.py tasks/<你的task.md>

# 下结论前自检：退出码 1 = 有证据缺口，不许给根因
python3 .codebuddy/skills/question-diagnosis/scripts/flow_guide.py tasks/<你的task.md> --check

# 看所有进行中的排查
python3 .codebuddy/skills/question-diagnosis/scripts/flow_guide.py --list
```

引导器输出：**当前阶段 / 进度 / 下一步做什么 / 怎么做 / 证据缺口清单 / `can_write_code=false` 约束 / 硬约束提醒**。

## 流程要点

1. **Step 0 搜集问题**：只收事实（现象/时间窗/环境/ID），写入「问题事实」节，此步禁止写根因猜想
2. **Step 1 分析疑点**：列全部候选假设，每条标注 🟡未验证/🟢已证实/🔴已排除
3. **Step 2 写 task.md**：每个疑点一条假设 = 假设 + 验证方法 + 预期证据（先写死证实/证伪标准）
4. **Step 3 逐条排查**：只信真实证据（日志原文/DB 值/代码行，带出处），[推断] 与证据严格区分
5. **Step 4 复盘**：未定位则基于新证据开下一轮（旧假设标 🔴 + 证伪依据）；**禁止无新证据的重复猜测**
6. **Step 5 结论**：完整定位推论文档（根因链每跳有证据 + 排除项 + 分级方案 + 弯路复盘）
7. **Step 6 归档**：会复现/可能再遇 → 提炼 `references/{domain}-{issue}.md` + 更新 `knowledge-map.md` 与 `SKILL.md` 索引，然后本目录可清理；一次性且确定不复现 → 不归档

## 门禁（`--check` 会拦什么）

- 标 🟢/🔴 的假设，证据段落**没有可复查出处**（需含 `文件:行号` / `[索引集+时间窗]` / `trace_id` / 代码块 之一）
- 已判定的假设**缺「验证方法」或「预期证据」**（判定标准必须事先写死）
- 写了结论但**没有任何 🟢 假设支撑**

## 目录清单

用 `flow_guide.py --list` 查看所有进行中的排查；已定位完成的目录可归档或清理。
