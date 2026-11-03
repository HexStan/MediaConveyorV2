# 提交与发布指南

本指南供 Agent 在本仓库执行「提交变更」与「发布版本」。两个流程均**仅在维护者主动要求时执行**（触发条件见 AGENTS.md「提交与发布」一节），不要自行触发。

## 通用约定

- 提交信息遵循 Conventional Commits + 中文描述，可带 scope，如 `fix: 修复规则匹配对于目录的逻辑缺陷`、`feat(config): 将 conflict_policy 改为选填并默认 "skip"`。常用类型：`feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `ci`；拿不准时参考 `git log` 近期风格。
- 版本号只写在根目录 `pyproject.toml` 的 `[project].version` 字段（单值）：`[x].[y].[z]` 为正式版，带 `-dev` 后缀为开发版。
- `gh` CLI 已认证（HexStan），创建 Release 与触发工作流均通过 `gh` 完成。
- 流程开始前先同步与验证：

  ```bash
  git pull --ff-only        # 分叉或失败时停止并报告维护者
  python -m pytest          # 涉及代码/测试的变更必须先通过
  ```

## 提交流程

前置条件：`git status` 存在未提交的变更；否则告知维护者无需提交，流程结束。

1. 用 `git status` 和 `git diff`（未跟踪文件直接阅读）审查全部待提交内容。
   - 疑似临时产物、密钥等不应入库的文件：不提交，向维护者报告。
2. 判断变更类型。**若存在多种不同类型的变更，按类型拆分为多次提交**（如功能代码与 README 更新拆为 `feat(...)` 与 `docs(...)` 两次）；同一类型内的关联变更合并为一次提交即可。
3. 逐次 `git add <文件>` + `git commit`，直至工作区干净。

本流程仅提交到本地，不推送；推送仅出现在发布流程中，或由维护者明确要求时执行。

> 作为发布流程第 1 步调用时，排除 `pyproject.toml`、`CHANGELOG.md` 与 `uv.lock`——这三个文件固定由发布流程第 3 步合并提交。

## 发布流程

前置条件：`pyproject.toml` 的 version 字段存在未提交的变更。若无，先与维护者确认目标版本号（例如将当前开发版号去掉 `-dev` 后缀），修改 `pyproject.toml` 后继续（该修改即成为未提交变更）。

发布路径由版本号是否带 `-dev` 后缀决定：

- 版本号带 `-dev` 后缀 → 开发版路径：第 1–4、6 步。
- 版本号不带 `-dev` 后缀 → 正式版路径：第 1–6 步。

### 第 1 步：提交变更

执行[提交流程](#提交流程)，但排除 `pyproject.toml` 与 `CHANGELOG.md`。

### 第 2 步：更新 CHANGELOG.md

只收录**用户可感知的变更**（影响行为、配置、日志输出、使用方式等）；纯重构、测试调整、CI 改动等不改变程序行为的变更不写入。分类子标题顺序固定：`### 破坏性变更` → `### 新增` → `### 修复` → `### 变更`，仅保留有内容的分类。存在破坏性变更时，可在该版本段落末尾追加 `### 迁移指南` 小节，给出旧配置对照、迁移步骤等用户需要的操作说明。

本步编写完成后停下，提示维护者查看编写完成的 `CHANGELOG.md`，**得到许可后**方可进入第 3 步提交。维护者要求调整时，修改后再次请其确认。

#### 开发版（版本号带 `-dev` 后缀）

1. 确定上个 tag（`git describe --abbrev=0 --match='v*'`，不排除 `-dev` tag——上个 tag 是开发版或正式版均可作为分界），分析 `git log --oneline <tag>..HEAD` 及相关提交内容。
2. 将结果**合并**进 `## 未发布` 段落：与现有条目去重；发现此前遗漏的用户可感知变更一并补充。最终以「`未发布` 段落完整覆盖自上个正式版以来的全部用户可感知变更、且无重复条目」为准。

#### 正式版（版本号不带 `-dev` 后缀）

1. 确定上个正式版 tag（`git describe --abbrev=0 --match='v*' --exclude='*-dev'`），分析 `git log --oneline <tag>..HEAD`，以 `## 未发布` 段落为底稿核对、补全。
2. 将发布内容写入新段落 `## [x.y.z] - YYYY-MM-DD`（日期为发布当日），置于 `## 未发布` 之后、上一个正式版段落之前；仅包含用户可感知的变更。
3. 清空 `## 未发布` 段落的条目（保留标题），供下个开发周期累积。
4. 在文件底部链接区最上方追加一行：`[x.y.z]: https://github.com/HexStan/MediaConveyor/releases/tag/vx.y.z`。

### 第 3 步：合并提交 pyproject.toml、CHANGELOG.md 与 uv.lock

前置条件：第 2 步的 CHANGELOG 已获维护者许可。

1. 先运行 `uv lock`：`uv.lock` 记录了本包自身的 version，`pyproject.toml` 的版本号变更会使锁文件过期。若不随本次提交收录，之后运行 `uv sync` / `uv run` 会产生意料之外的 `uv.lock` 改动。uv 会将版本号规范化为 PEP 440 形式（`*-dev` 写为 `*.dev0`）；若锁文件本已同步，此命令无变更。
2. 将 `pyproject.toml`、`CHANGELOG.md` 与 `uv.lock`（如有变更）合并为**一次**提交，信息沿用既有风格：`docs: 提升版本号，更新日志`。

### 第 4 步：打 tag 并推送

1. 打附注 tag，指向第 3 步的提交；tag 名为 `v` + 版本号（`pyproject.toml` 的 version 字段）。tag 已存在时停止并报告维护者：

   - 开发版（版本号为 `x.y.z-dev`）：`git tag -a vX.Y.Z-dev -m "Release vX.Y.Z-dev"`。
   - 正式版（版本号为 `x.y.z`）：`git tag -a vX.Y.Z -m "Release vX.Y.Z"`。

2. `git push origin <分支名> <tag>`（分支与 tag 一起推送）。

### 第 5 步（仅正式版）：创建 Release

标题为 `vX.Y.Z`，注记为 CHANGELOG 对应小节去掉 `## [x.y.z] - 日期` 标题行后的正文。

```bash
gh release create vX.Y.Z --title "vX.Y.Z" --notes-file release_notes.md
```

- 注记可先写入仓库根的临时文件 `release_notes.md`（勿提交，用后删除），提取命令见附录 B。
- 创建 Release 依赖第 4 步已推送的 tag：若跳过第 4 步直接执行，`gh` 会在默认分支（dev）的最新提交上另建同名 tag。

### 第 6 步：触发构建

#### Docker 镜像

Docker 镜像的构建与推送由 CI（`.github/workflows/docker-publish.yml`）完成，Agent 只需保证提交已推送：

- 分支与版本 tag 均已在第 4 步一并推送，本步无需再推送。
- CI 触发条件：推送且 `pyproject.toml` 的 version 字段相对上次推送有变更（任意分支均触发；`pyproject.toml` 的其他变更——如 Dependabot 依赖更新——不会触发）。若本次推送未变更 version（补发场景），手动触发：`gh workflow run docker-publish.yml --ref <分支名>`。
- 确认构建成功：`gh run list --workflow=docker-publish.yml --limit 1`（或 `gh run watch`）；失败时报告维护者。

构建以仓库根为 context（`docker build -f docker/app/Dockerfile .`），产物推送至 GHCR（镜像名 = 仓库名全小写）：

| 版本号 | 镜像标签 |
| --- | --- |
| `x.y.z-dev` | `x.y.z-dev`、`dev` |
| `x.y.z`（正式版） | `x.y.z`、`latest` |

#### 可执行文件

打包版可执行文件由 CI 自动构建，Agent 只需保证第 4–5 步完成：

- 正式版：Release 创建（第 5 步）后，`.github/workflows/executable-release.yml` 自动构建 Windows/macOS × AMD64/ARM64 四个平台的 zip（内置 rclone）并附加至该 Release 的 Assets。
- 开发版：任意分支推送带 `-dev` 后缀的 version 变更时，`.github/workflows/executable-dev.yml` 自动构建，产物仅输出至工作流 Artifacts（不发布）。
- 确认构建成功：`gh run list --workflow=executable-release.yml --limit 1`（或 `executable-dev.yml`）；失败时报告维护者。

## 附录 A：CHANGELOG.md 格式

```markdown
# 更新日志

---

## 未发布

### 破坏性变更

- …

---

## [x.y.z] - YYYY-MM-DD

### 新增

- …

### 修复

- …

### 变更

- …

### 迁移指南（可选，仅存在破坏性变更时）

#### 1. …

---

[x.y.z]: https://github.com/HexStan/MediaConveyor/releases/tag/vx.y.z
```

- 条目为一句话中文描述，面向用户；段落之间以 `---` 分隔。
- 正式版段落日期为发布当日；文件底部链接区新版本置于最上方。

## 附录 B：提取发布注记（可选）

```bash
VERSION=x.y.z
python - "$VERSION" <<'PY'
import re, sys
version = sys.argv[1]
content = open('CHANGELOG.md', encoding='utf-8').read()
m = re.search(rf'## \[{re.escape(version)}\].*?(?=\n---|\n## \[|\n\[\d|\Z)', content, re.DOTALL)
notes = m.group(0).strip().split('\n', 1)[1] if m and '\n' in m.group(0) else ''
open('release_notes.md', 'w', encoding='utf-8').write(notes.strip())
PY
```

## 异常与中止

遇以下情况停止流程并向维护者报告，不要自行变通：

- `python -m pytest` 未通过；
- 待提交内容混有疑似不应入库的文件；
- `uv lock` 执行失败（此时第 3 步尚未提交，可修复后重跑第 3 步）；
- 目标 tag 已存在；
- `gh` 认证或网络异常导致 Release 创建失败（此时提交与 tag 已推送可保留，修复后仅重跑第 5 步）。
