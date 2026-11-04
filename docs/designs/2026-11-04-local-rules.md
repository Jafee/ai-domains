# 加入本地附加规则 local-rules.txt，并用它拦截 aws-external-anthropic.{region}.api.aws

日期：2026-11-04 ｜ 分支：`feat/local-rules` ｜ 状态：实现中

## 背景与问题

`adguard.txt` 完全由 `scripts/update.py` 从上游 v2fly `category-ai-!cn` 生成，每周 GitHub Actions 重新生成并覆盖。用户要拦截 `aws-external-anthropic.<region>.api.aws` (Anthropic 在 AWS 上的 API 入口，`<region>` 是 `us-east-1` 这类单个标签)，上游 `anthropic` 列表里没有它。手工改 `adguard.txt` 会在下一次每周更新时被冲掉，而且 CI 的 `--check` 会报文件过期，所以需要一个由生成器读取、随仓库提交的本地规则文件。

## 方案

1. **新文件 `local-rules.txt`** (仓库根目录)，语法与上游 v2fly 数据文件相同：每行一条 `domain:` / `full:` / `regexp:` / `keyword:` 或裸域名，`#` 开头或行内 `#` 之后是注释。本次只放一条：
   ```
   regexp:^aws-external-anthropic\.[a-z0-9-]+\.api\.aws$
   ```
   它按现有 `regex_rule()` 转换后应输出 `/(?:^|\.)aws-external-anthropic\.[a-z0-9-]+\.api\.aws$/`，即拦截这个主机及其子域名，`<region>` 只匹配一个标签。
2. **`scripts/update.py`**：
   - 新常量 `LOCAL_RULES = REPO_ROOT / "local-rules.txt"`。
   - `build_artifacts(snapshot, local="")` 增加参数 `local` (本地文件的文本，默认空串，所以现有调用方行为不变)。本地文本先过 `canonical_source()`，再用现有 `parse_sources()` 解析，转换走现有 `entry_rule()`，与上游规则合并进同一个去重集合后排序输出。
   - 本地条目**必须拒绝** (抛 `UpdateError`)：`include:`、任何 `@属性` 或 `&分类` 注解。理由：本地文件只用来补具体主机，不做列表组合。
   - `main()` 读取 `LOCAL_RULES` 传给 `build_artifacts`；文件不存在时失败 (返回 1，旧输出不动)，与项目「残缺就不发布」的原则一致。联网更新和 `--snapshot` 两条路径都读它。
   - `adguard.txt` 头部在 `! Full-host rules intentionally include subdomains.` 之后加一行 `! Local additions: <N> rules from local-rules.txt`；`metadata.json` 加一个键 `local_rule_count`。`Rules:` 总数和 `total_rule_count` 按合并去重后的结果算。
   - `sources.json` 不变，仍只存上游数据。本地文件本身在仓库里，`--snapshot sources.json --check` 照样能离线复现。
3. **`tests/test_update.py`**：
   - 必须新增测试：(a) 本地 regexp 条目进入输出，且转换结果与上面的字符串完全相等；(b) 它匹配 `aws-external-anthropic.us-east-1.api.aws` 和 `a.aws-external-anthropic.eu-west-2.api.aws`，不匹配 `aws-external-anthropic.api.aws`、`aws-external-anthropic.us-east-1.api.aws.evil.org`、`xaws-external-anthropic.us-east-1.api.aws`、`aws-external-anthropic.a.b.api.aws`；(c) 本地文件里出现 `include:`、`@属性`、`&分类` 时抛 `UpdateError`；(d) `LOCAL_RULES` 不存在时 `main()` 返回 1 且旧输出不变。
   - 现有测试里调用 `main()` 的 (`test_failed_update_keeps_all_existing_outputs`、`test_generation_is_reproducible_and_check_detects_changes`) 按需最小改动，让它们不依赖仓库里 `local-rules.txt` 的具体内容 (例如 `patch.object(update, "LOCAL_RULES", <临时文件>)`)。
   - `test_current_snapshot_has_complete_domain_and_regex_coverage` 改为同时读取仓库的 `local-rules.txt`，期望集合包含本地条目，从而覆盖真实文件。
4. **`.github/workflows/test.yml`**：`push` 和 `pull_request` 的 `paths` 各加 `local-rules.txt`，否则只改本地规则的提交不会触发 CI。
5. **`README.md`**：新增一小节说明 `local-rules.txt` 的用途、语法、限制 (不支持 include 和注解) 和改完要重新生成；把「不额外添加上游分类以外的服务」改成「除 `local-rules.txt` 里手工列出的条目外，不额外添加……」。
6. **重新生成** `adguard.txt` 与 `metadata.json`：`python3 scripts/update.py --snapshot sources.json` (离线，用已提交的上游快照，不拉新上游)。

## 工作契约

- **Goal**：合并后 `adguard.txt` 含 `/(?:^|\.)aws-external-anthropic\.[a-z0-9-]+\.api\.aws$/`，每周自动更新会保留它。
- **Verification** (命令取自 `.github/workflows/test.yml`)：
  - `python3 -m unittest discover -s tests -v` 全部 OK (基线 21 个测试，新增后更多)。
  - `python3 scripts/update.py --snapshot sources.json --check` 输出 `Verified 215 rules from 23 source files.`
  - `grep -c 'aws-external-anthropic' adguard.txt` 输出 `1`。
- **Done**：以上三条通过、codex APPROVE、PR 的 CI 全绿。合并后在 AdGuard Home 服务器上刷新订阅、确认 `check_host` 命中，属于部署，不在本 PR 内。
- **评审档位**：单审 (只跑 codex)。理由：改动小、逻辑直白，最坏后果是订阅里多拦或少拦几个域名，测试能抓到，可随时回滚；不属于 Hard Rules 的高风险档。
- **Fable 参与**：不参与。设计者是主线程 (Opus)。理由：不涉及并发、持久化或不兼容的格式变更，复杂度低。

## 范围契约

```ship-scope
base: 187e2b5928b16659c6c2b7c83ccd09539b3b9a5f
max_files: 7
max_added_lines: 100
max_new_files: 1
allow_paths:
  - scripts/update.py
  - tests/test_update.py
  - local-rules.txt
  - README.md
  - .github/workflows/test.yml
  - adguard.txt
  - metadata.json
# 流程产物不计入预算(脚本默认也排除,但契约里必须写明,否则评审者按字面会判越界)
exclude_paths:
  - docs/designs/**
  - IMPLEMENTATION_PLAN.md
forbid_new_deps: true
```

用 `python3 ~/.claude/skills/ship/scripts/budget_check.py <本文档> --repo <worktree>` 校验，退出码 1 表示超预算。

**明确不做的事**：

- 不改 `.github/workflows/update.yml` (它每次都跑，已经会读到本地文件)。
- 不改 `sources.json`，不拉取新的上游数据。
- 不重构 `parse_sources()`、`regex_rule()` 等现有转换逻辑；不改上游规则的转换结果。
- 不加 IP 规则 (IP 段由 AdGuard Home 服务器的自定义规则负责，不进这个订阅)。
- 不引入新依赖，不加文档没要求的防护或配置项 (例如命令行参数 `--local-rules`)。

## 风险

- 本地正则写错会误拦：测试 (b) 用正反例钉住匹配范围。
- 回滚：revert 这个 PR，再跑一次 `update.py` 即可，订阅下次刷新时恢复。

## 契约修订记录

(暂无)

## 评审记录

(暂无)

## 后续建议

- 可以把 `aws-external-anthropic` 这条提交给上游 v2fly 的 `anthropic` 列表，上游收录后删掉本地这条。
