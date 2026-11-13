---
name: milestone-git
description: mooapp 里程碑的 Git 操作：开分支、按「先测试后实现」分批提交、验收通过后合并到 main 并打 tag、推送。用户说 /milestone-git，或要求为某个里程碑（M1–M9）开分支、提交、打 tag、合并、推送时使用。
argument-hint: "<start|commit|finish|push|status> <N>"
disable-model-invocation: true
allowed-tools: Bash(git status:*), Bash(git log:*), Bash(git diff:*), Bash(git branch:*), Bash(git tag:*), Bash(git rev-parse:*), Bash(git add:*), Bash(bash .claude/skills/milestone-git/scripts/preflight.sh:*)
---

# 里程碑 Git 操作（mooapp）

参数：`$ARGUMENTS`，第一个词是子命令，第二个词是里程碑编号 N（如 `commit 4`）。缺编号时，从当前分支名 `dev/milestone_N` 推断；推断不出来就问用户。

## 命名

分支与 tag 用不同前缀，避免同名歧义：

| 对象 | 名字 | 例 |
|---|---|---|
| 里程碑分支 | `dev/milestone_N` | `dev/milestone_4` |
| 验收通过的 tag | `accept/milestone_N` | `accept/milestone_4` |

## 本仓库的约定（来自 git 历史与 PROGRESS.md）

- 每个里程碑一个分支 `dev/milestone_N`，从 `main` 拉出。
- 提交分批，顺序是**先测试、后实现**，与 CLAUDE.md「先写失败的测试」一致：
  1. `test(mN): …`：测试、fixture、文档（规格、映射表）、探针
  2. `feat(mN): …`：src/、db/ 等实现，以及 learnings、PROGRESS
  3. 可选 `docs(mN): …`：只改文档的收尾
- 提交说明用英文祈使句做标题，正文用 `- ` 列出要点；最后一行是 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`（以会话里系统给出的署名为准）。
- 验收通过后，`main` 上用 `--no-ff` 合并（提交说明为 `Merge branch 'dev/milestone_N'`），**tag `accept/milestone_N` 打在合并提交上**。
- 合并、打 tag 后**保留分支 `dev/milestone_N`**（用户要留着学习：在分支上能看到一个里程碑从测试到实现的完整过程）。不要删除，也不要建议删除。

## 硬性规则

- **提交由用户用 `!` 执行。** pre-commit 钩子（gitleaks）要写 `~/.cache/pre-commit/`，sandbox 不允许，在 sandbox 里提交必然失败。Claude 负责 `git add`（暂存）和写好提交说明，然后把完整的 `! git commit …` 命令交给用户。
- **绝不使用 `--no-verify`**，也不修改 pre-commit 的缓存位置或配置：那等于绕过密钥扫描。
- **push 由用户用 `!` 执行**（sandbox 拦 git 的网络操作）。给出命令之前，先用 AskUserQuestion 确认，写明分支、tag、远程（本机管理员规则：`git push` 先问）。
- 不改写历史：不用 `rebase`、`reset --hard`、`commit --amend`、`push --force`；不删除 tag，也不删除分支。需要时停下来问用户。
- 预检和排查只用只读命令，不往 `.git/` 写任何东西（例如不用 `git hash-object -w`、不改索引来做模拟）。
- 不暂存这些文件：`config/*.local.toml`、`config/secrets.toml`、`.env*`、`*.pem`、`*.key`、`recordings/`、`var/`、`reports/`。`tests/acceptance/` 只由用户修改：如果那里有改动，要向用户确认是他本人改的。
- 打 tag 之前必须确认 SPEC 8 节该里程碑的验收已经通过（用户跑的 `pytest -m sim` 和验收测试结果）。没确认就不打。

## 子命令

### `status N`：只看不改

1. 运行 `bash .claude/skills/milestone-git/scripts/preflight.sh N`
2. 用 3–6 行总结：当前分支、未提交的改动、比 main 领先几个提交、tag `accept/milestone_N` 是否存在、预检警告。

### `start N`：开分支

1. 运行预检。工作区不干净时停下，列出改动，问用户怎么处理（不要替用户 stash 或丢弃）。
2. 确认 `main` 已包含上一个里程碑：`git log --oneline main -5`，看有没有 `Merge branch 'dev/milestone_{N-1}'`。如果没有，告诉用户，问是从 `main` 拉，还是从 `dev/milestone_{N-1}` 拉（后者适合上一个里程碑还没验收、先往前推进的情况）。
3. 本地创建分支不经过钩子，可以在 sandbox 里执行：`git switch -c dev/milestone_N <起点>`。执行前用一句话说明起点。
4. 提醒用户：PROGRESS.md 里写明「M{N} 计划（已确认 日期）」之后再开始写代码（CLAUDE.md：先给计划）。

### `commit N`：分批提交

1. 运行预检。有 ERROR 就停下。
2. `git status --short` 加 `git diff --stat`，把改动分成两组：测试与文档 / 实现。归哪一组拿不准的文件，列出来问用户。
3. 只暂存第一批：`git add <明确的路径>`。不要用 `git add -A` 或 `git add .`，避免带进不该提交的文件。暂存后用 `git diff --cached --stat` 给用户看。
4. 写提交说明（格式见上面的约定），交给用户一条完整命令，用多个 `-m` 分段，避免 heredoc 引号出错：
   ```
   ! git commit -q -m "test(mN): <标题>" -m "- 要点 1
   - 要点 2" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
   ```
5. 用户执行后，`git log --oneline -3` 确认提交已生成，再暂存第二批，重复第 4 步。
6. 如果用户贴回钩子报错（例如 gitleaks 发现疑似密钥）：不要绕过。指出是哪个文件、哪一行（凭证用 `***` 遮盖），问用户怎么处理。

### `finish N`：验收后合并并打 tag

1. 用 AskUserQuestion 确认：SPEC 8 节 M{N} 的每一条验收都通过了吗？请用户贴结果，或说明是哪次运行。任一条没通过就停下。
2. 运行预检：当前在 `dev/milestone_N`，工作区干净。
3. 列出将要执行的命令，再用 AskUserQuestion 确认一次：
   ```
   git switch main
   git merge --no-ff dev/milestone_N -m "Merge branch 'dev/milestone_N'"
   git tag accept/milestone_N
   ```
   分支 `dev/milestone_N` 保留。合并提交可能触发钩子。如果在 sandbox 里失败，把这三条改成 `!` 命令交给用户执行，不要换别的写法绕过。
4. 完成后 `git log --oneline --graph -6`、`git tag -n1` 和 `git branch` 给用户确认：tag 在合并提交上，分支仍在。
5. 提醒：PROGRESS.md 的「当前 / 下一步」要更新（可以作为下一个里程碑的第一个提交）。

### `push N`：推送

1. 用 AskUserQuestion 确认要推的内容：`main`、tag `accept/milestone_N`，要不要同时推分支 `dev/milestone_N`（保留在远程方便回看），远程 `origin`。
2. 交给用户执行（按确认结果增减）：
   ```
   ! git push origin main && git push origin accept/milestone_N && git push origin dev/milestone_N
   ```
   只推这一个 tag，不用 `--tags`，避免把本地的临时 tag 一起推上去。
3. 用户贴回结果后，用 `git status -sb` 确认 `main` 与 `origin/main` 一致。

## 汇报格式

先用一句话给结论，再列出：执行了哪些命令及结果；交给用户执行的命令；需要用户决定的事（给 2–4 个选项）。
