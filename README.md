# ai-domains

将 [v2fly/domain-list-community 的 category-ai-!cn](https://github.com/v2fly/domain-list-community/blob/master/data/category-ai-!cn) 转成 AdGuard Home 的 DNS 屏蔽订阅，每周自动更新。

## 订阅地址

公开 GitHub 仓库：[Jafee/ai-domains](https://github.com/Jafee/ai-domains)。订阅地址为：

```text
https://raw.githubusercontent.com/Jafee/ai-domains/main/adguard.txt
```

在 AdGuard Home 的「过滤器 → DNS 黑名单 → 添加黑名单 → 添加自定义列表」中填写该地址。订阅者只需要 `adguard.txt` 的 Raw 地址。

## 自动更新

- `.github/workflows/update.yml`：每周一北京时间／新加坡时间 **08:17** 自动运行，也可在 Actions 的 **Update AI domains → Run workflow** 手动触发。
- 先运行测试，再从上游 `master` 获取最新 commit，读取该 commit 的数据归档，生成 `adguard.txt`、`sources.json` 和 `metadata.json`。
- 输出发生变化时，使用 GitHub 自动提供的仓库令牌提交到默认分支。无需配置个人访问令牌或额外 Secrets。
- 下载、解析或校验失败时任务失败，GitHub 中的上一版订阅文件保持可用。

第一次上传后，手动运行一次 **Update AI domains**，将附带的初始快照更新成固定 commit 的最新数据。工作流需要放在默认分支，并允许 `contents: write`。如果仓库规则要求通过 PR 合并或组织禁止 Actions 写入，需允许这个更新任务直接提交，才能按当前方式自动更新。

[GitHub 定时任务](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)可能延迟；公开仓库连续 60 天没有活动时，定时工作流可能被自动禁用，可在 Actions 页面重新启用。

## 转换规则

| 上游语法 | AdGuard 输出 |
| --- | --- |
| 裸域名、`domain:example.com` | `\|\|example.com^` |
| `full:api.example.com` | `\|\|api.example.com^`，按本仓库要求同时屏蔽子域名 |
| `include:列表名` | 递归展开，检测缺失文件和循环引用 |
| `include:列表名 @属性 @-属性` | 只包含符合全部属性条件的条目 |
| `&分类名` | 处理上游条目对分类的归属，包括没有同名文件的分类 |
| `keyword:` | 转成域名匹配正则 |
| `regexp:` | 转成 AdGuard 正则，将主机名起始锚点扩展为域名层级边界 |

`||example.com^` 屏蔽域名本身及其任意层级子域名，见 [AdGuard 官方语法](https://adguard-dns.io/kb/general/dns-filtering-syntax/)。不额外添加上游分类以外的服务，也不将具体 CDN 主机扩大成整个 CDN 的根域名。上游若出现无法正确转换的语法，更新会失败并保留旧版本，不会跳过条目后发布残缺列表。

## 本地生成和验证

仅使用 Python 3.10+ 标准库，无需安装第三方包。

```sh
# 拉取最新上游并生成订阅
python3 scripts/update.py

# 从仓库内快照离线重建
python3 scripts/update.py --snapshot sources.json

# 检查生成文件是否与快照一致
python3 scripts/update.py --snapshot sources.json --check

# 运行测试
python3 -m unittest discover -s tests -v
```

`metadata.json` 记录规则数量、上游 commit 和文件哈希；`sources.json` 保存本次生成用到的规则数据，可离线复现。附带的初始快照获取于 2026-10-05，尚未固定上游 commit；第一次联网更新会补齐。

上游域名数据来自 v2fly/domain-list-community，采用 MIT 许可，原许可见 [licenses/v2fly-domain-list-community.txt](licenses/v2fly-domain-list-community.txt)。
