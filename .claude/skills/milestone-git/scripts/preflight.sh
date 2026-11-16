#!/bin/bash
# milestone-git 预检（只读）：不修改工作区、索引、分支或 tag。
# 用法：bash .claude/skills/milestone-git/scripts/preflight.sh <N>
# 退出码：0 = 无 ERROR（可能有 WARN）；1 = 有 ERROR（暂存了敏感文件等）；2 = 用法错误
# 注意 macOS 自带 bash 3.2：变量后紧跟全角字符会被当成变量名的一部分，一律写成 ${var}（learnings「阶段 0 实测」）。

set -u

N="${1:-}"
if [ -z "${N}" ]; then
  branch_now="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  case "${branch_now}" in
    dev/milestone_[0-9]*) N="${branch_now#dev/milestone_}" ;;
    *) echo "用法：preflight.sh <N>（当前分支 ${branch_now} 推断不出里程碑编号）"; exit 2 ;;
  esac
fi
BR="dev/milestone_${N}"     # 里程碑分支（合并后保留）
TAG="accept/milestone_${N}" # 验收通过的 tag
errors=0

echo "== 里程碑 ${N} 预检（分支 ${BR}，tag ${TAG}）=="

branch="$(git rev-parse --abbrev-ref HEAD)"
echo "当前分支：${branch}"
if [ "${branch}" != "${BR}" ]; then
  echo "WARN 当前分支不是 ${BR}"
fi

if git rev-parse --verify --quiet "refs/heads/main" >/dev/null; then
  ahead="$(git rev-list --count main..HEAD)"
  behind="$(git rev-list --count HEAD..main)"
  echo "相对 main：领先 ${ahead} 个提交，落后 ${behind} 个提交"
fi

if git rev-parse --verify --quiet "refs/tags/${TAG}" >/dev/null; then
  echo "tag ${TAG}：已存在（$(git rev-parse --short "refs/tags/${TAG}")）"
else
  echo "tag ${TAG}：不存在"
fi

if git rev-parse --verify --quiet "refs/heads/${BR}" >/dev/null; then
  echo "分支 ${BR}：存在"
else
  echo "分支 ${BR}：不存在"
fi

echo "-- 工作区 --"
status="$(git status --short)"
if [ -z "${status}" ]; then
  echo "干净"
else
  echo "${status}"
fi

echo "-- 已暂存文件检查 --"
staged="$(git diff --cached --name-only)"
if [ -z "${staged}" ]; then
  echo "没有已暂存的文件"
else
  echo "${staged}" | while IFS= read -r f; do
    case "${f}" in
      config/*.local.toml|config/secrets.toml|.env|.env.*|*.pem|*.key|recordings/*|var/*|reports/*)
        echo "ERROR 不应提交：${f}" ;;
      tests/acceptance/*)
        echo "WARN 验收测试有改动：${f}（只能由用户本人修改，请确认）" ;;
      config/settings.toml)
        echo "WARN config/settings.toml 有改动：确认没有动风控限额（CLAUDE.md 硬性规则）" ;;
      .claude/*)
        echo "WARN .claude/ 有改动：${f}（Claude Code 配置，应由用户本人修改）" ;;
    esac
  done | tee "${TMPDIR:-/tmp}/milestone-git-preflight.$$"
  if grep -q '^ERROR' "${TMPDIR:-/tmp}/milestone-git-preflight.$$"; then
    errors=1
  fi
  rm -f "${TMPDIR:-/tmp}/milestone-git-preflight.$$"
fi

echo "-- 最近提交 --"
git log --oneline -5

if [ "${errors}" -ne 0 ]; then
  echo "== 预检失败：先把上面标 ERROR 的文件移出暂存区（git restore --staged <文件>）=="
  exit 1
fi
echo "== 预检完成 =="
exit 0
