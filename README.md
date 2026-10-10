# Paper Crawler · 论文关键词爬虫

从指定会议／期刊来源提取标题、摘要、作者、DOI 等元数据，按关键词筛选，下载 PDF，并可导入本地 Zotero。

支持配置化来源、关键词组合筛选、处理预算、缓存恢复及 Zotero 本地 API 导入。来源范围与会议日期由配置明确指定。

## 环境要求

- Python 3.10 或更新版本。
- 能访问所配置论文来源的网络；离线重筛需要已有缓存。
- 下载或仅抓元数据无需 Zotero；本地写入集成需要 Zotero 10+，详见 [Zotero](#zotero)。

## 安装与首次运行

以下命令在下载并解压的源码目录或克隆后的仓库根目录执行。`python` 应指向 Python 3.10+；Windows 也可使用 `py -3` 创建虚拟环境。

### Windows PowerShell

直接使用虚拟环境中的解释器，无需修改 PowerShell 执行策略：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

# 仅首次创建配置，保留已有配置
if (-not (Test-Path -LiteralPath config.json)) {
    Copy-Item config.example.json config.json
}
.\.venv\Scripts\python.exe -m paper_crawler --help
```

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# 仅首次创建配置，保留已有配置
if [ ! -e config.json ]; then
    cp config.example.json config.json
fi
.venv/bin/python -m paper_crawler --help
```

如果已经使用 Conda 或其他 Python 环境，可以省略创建虚拟环境的步骤，在该环境中执行 `python -m pip install -e .`。下文使用 `python` 简写，Windows 虚拟环境用户可替换为 `.\.venv\Scripts\python.exe`，Linux/macOS 用户可替换为 `.venv/bin/python`。

### 配置与元数据试跑

编辑 `config.json`，按实际任务设置 `keywords`、`conferences` 中的来源及年份，并核实当年会议的 `meeting_dates`。第一次运行建议只保留一个会议、一个年份。配置中的相对路径以**配置文件所在目录**为基准。

示例配置默认启用 Zotero 且 `download_only=false`。首次试跑使用元数据模式：

```powershell
python -m paper_crawler --config config.json --metadata-only --max-papers 10 --max-seconds 60
```

该命令不下载 PDF、不连接 Zotero，但仍会更新本地缓存和运行报告。报告中的零匹配可能由关键词、扫描预算或来源状态造成，需要结合失败记录判断。

### 下载与导入

选择所需模式后，再运行下载命令：

| 模式 | 设置 | 行为 |
|---|---|---|
| 仅元数据 | 命令行 `--metadata-only` | 筛选并保存元数据，不下载、不连接 Zotero |
| 仅下载 | 配置 `metadata_only=false`、`download_only=true` | 下载 PDF，不写入 Zotero |
| 下载并导入 | 配置 `metadata_only=false`、`download_only=false`、`zotero.enabled=true` | 下载 PDF，并通过本地 API 导入 Zotero |

```powershell
python -m paper_crawler --config config.json --max-matches 10 --max-papers 300 --max-seconds 300
```

也可使用安装生成的 `paper-crawler` 命令，其参数与 `python -m paper_crawler` 相同。虚拟环境未激活时，需要使用命令的完整路径，例如 Windows 的 `.\.venv\Scripts\paper-crawler.exe`。

程序退出码：`0` 全部完成或正常达到预算；`1` 配置错误或全局错误；`2` 存在部分失败；`130` 用户通过 Ctrl+C 中断。网站零结果也会记录为失败，避免把解析失效误认为没有论文。

## 小批量试跑：限制处理数量

```powershell
python -m paper_crawler --config config.json --max-matches 10 --max-papers 300 --max-seconds 300
```

也可以在配置文件顶层设置 `"max_matches": 10`。命令行优先于配置；不设置或配置为 `null` 表示不限量，`--max-matches 0` 可以覆盖配置中的限额，恢复不限量。只接受非负整数，不接受负数、小数、字符串形式的 JSON 数字或布尔值。

- `max_matches` 是**本轮待处理的匹配论文数**，跨会议、年份全局累计。默认按来源年份轮换，不会给每个会议单独分配名额。
- 仅下载模式下，有有效本地 PDF 的论文不占名额；导入模式下，有有效 PDF 且已导入当前 Zotero 文献库的论文不占名额。
- 同一篇论文下载并导入只计一次，失败也占一次。相同 stable ID 在同一次运行中最多尝试一次；失败论文可在下次运行重试。
- 设置限额时，CVF 会优先检查当前列表中标题命中关键词的论文，再继续检查其余论文的摘要；这是顺序调整，不会丢弃仅摘要命中的候选。每扫描 25 篇会记录进度，处理和下载匹配论文也会记录日志。
- 限额不是扫描数量或 HTTP 请求数量；未命中和已完成论文仍可能需要读取详情才能识别，不占处理名额。
- 达到限额后不再读取下一篇详情或进入下一来源。报告中的 `stop_reason` 为 `max_matches_reached`，`stats.attempted` 给出实际尝试数；本身不是错误，若没有其他失败则退出码为 0。
- 提前停止的来源会保留已解析元数据和页面缓存，但不会写成完整快照；已有的完整快照保持原样。即使恰好在最后一篇用完额度，也保守地视为提前停止，不额外读取下一篇确认结束。
- 重复运行相同命令会跳过已完成论文并处理下一批。失败论文仍会占下一轮重试额度。

三个预算中最先达到的一个会停止运行，**不保证下载满 10 篇**：

| 参数 | 含义 |
|---|---|
| `--max-matches 10` | 最多处理 10 篇待处理的匹配论文，失败也占名额 |
| `--max-papers 300` | 最多检查 300 个论文候选，包含未命中、缓存读取和详情失败；成功记录的重复 ID 不重复计数 |
| `--max-seconds 300` | 为本轮设置 300 秒时间预算，覆盖请求、限速等待、重试、下载和 Zotero 操作 |

对应 JSON 字段为 `max_matches`、`max_papers`、`max_seconds`；`0` 或 `null` 表示不限。示例配置设置了 300 篇扫描、300 秒时间预算；旧配置缺少新字段时仍不限。扫描上限不是 HTTP 请求数上限，列表分页也会产生请求。

时间预算通过各操作检查和缩短网络超时执行，不是强制杀进程计时器；单次阻塞 I/O、DNS 或较长解析可能导致实际结束时间略超预算。Ctrl+C 会保留已提交元数据、清理下载临时文件并输出报告；强制终止进程仍保留已提交缓存，但不保证生成本轮最终报告。

默认 `scheduling="round_robin"`、`source_batch_size=5`、`newest_first=true`：全局按年份从新到旧处理，同年来源每检查 5 个候选后轮换；当前年份的可用任务全部结束后才进入上一年。可用 `--scheduling sequential` 逐个来源年份处理，用 `--source-batch-size 1` 更频繁地轮换。`newest_first=false` 恢复配置顺序下的调度。请求仍串行执行并遵守来源限速。

## 当年会议的结束日期

程序在读取缓存或访问论文站点之前检查会议日期。未来年份跳过；当年会议必须配置已核实的最终结束日期，且该日期已经过去，才会进入队列。未知日期不会推测为已结束。往年未配置日期的来源按历史会议处理；显式配置的日期始终检查。期刊（`item_type="journalArticle"`）不受会议结束日期规则约束，但仍跳过未来年份。

会议条目配置示例：

```json
{
  "years": [2026, 2025],
  "meeting_dates": {
    "2026": {
      "end_date": "2026-06-07",
      "source_url": "https://cvpr.thecvf.com/Conferences/2026/Dates"
    }
  }
}
```

`end_date` 是整个会议（含最后一天 Workshop）的结束日期，不是投稿截止、录用通知或论文上线日期。为避免在会议所在地当天尚未结束时提前抓取，统一等到结束日期在 UTC−12 也完全过去后放行。判断使用运行机器的 UTC 时间，与系统显示时区无关。

以下为 2026-09-22 核对官网时记录的日期配置快照；“当日处理”指该核对日期的调度结果，不代表当前运行结果：

| 会议 | 最终结束日期 | 当日处理 |
|---|---|---|
| [AAAI 2026](https://aaai.org/conference/aaai/aaai-26/) | 2026-01-27 | 纳入 |
| [ICLR 2026](https://iclr.cc/Conferences/2026) | 2026-04-27 | 纳入 |
| [CVPR 2026](https://cvpr.thecvf.com/Conferences/2026/Dates) | 2026-06-07 | 纳入 |
| [ACL 2026](https://2026.aclweb.org/) | 2026-07-07 | 纳入 |
| [ICML 2026](https://icml.cc/Conferences/2026/CallForPapers) | 2026-07-11 | 纳入，使用官方 OpenReview 录用论文入口 |
| [NeurIPS 2026](https://neurips.cc/Conferences/2026/Dates) | 2026-12-13（所有会场最终结束） | 跳过 |
| [ICCV 2025](https://iccv.thecvf.com/Conferences/2025/Dates) | 2025-10-23 | 在 2025 年批次处理 |

ICML 2024/2025 保留 PMLR 来源；上述核对时 PMLR 目录未列出 ICML 2026 卷号，因此示例中该年份单独配置 OpenReview。会议已结束不保证元数据源已发布或可访问，抓取失败仍记录失败。

日期表是带官方依据的显式配置，不会每次运行自动解析官网日期；会议延期或新增年份时需要核实并更新 `years` 和 `meeting_dates`，不能直接套用上一年的日期。报告的 `schedule_checked_at` 记录判断时间，`skipped_sources` 区分 `future_year`、`conference_not_finished`、`unknown_meeting_end_date`；日期跳过不算网络失败，也不消耗扫描额度。

只验证解析、筛选和调度，可运行：

```powershell
python -m paper_crawler --config config.json --metadata-only --max-papers 10 --max-seconds 60
```

`metadata_only=true` 不下载 PDF、不连接 Zotero，但会保存元数据缓存和报告；此模式下已下载论文的匹配结果也计入处理额度。

## 来源与论文范围

| adapter | 来源 | 范围与配置 |
|---|---|---|
| `cvf` | CVPR / ICCV | 遍历分日页面、去重，从详情页读取摘要、作者、PDF |
| `openreview` | ICLR / NeurIPS | 默认通过 `content.venueid` 只取正式录用论文，读取返回的 PDF 字段 |
| `neurips` | NeurIPS 官方论文集 | 读取标题、摘要、作者及 PDF，默认只取 main track |
| `acl_anthology` | ACL | 默认仅当前年份的 `acl-long` 和 `acl-short`；排除卷封面／前言 |
| `pmlr` | ICML | 使用论文条目选择器；2025 年默认卷号为 267 |
| `aaai` | AAAI OJS | 遍历归档分页，通过详情页 citation 元数据提取 PDF |
| `generic_html` | 可配置会议或期刊 | CSS 选择器、分页、可选详情页和期刊条目类型 |

“顶刊／顶会”名单由 `conferences` 中显式配置的来源决定，程序不自动判定排名。不同会议的 Workshop、Findings、特殊 track 不应混用主会配置。

OpenReview 推荐配置：

```json
{
  "name": "ICLR",
  "website": "https://openreview.net",
  "adapter": "openreview",
  "years": [2025],
  "options": {
    "venue_id_template": "ICLR.cc/{year}/Conference",
    "accepted_only": true
  }
}
```

`accepted_only=false` 会包含非录用投稿，通常不适合正式论文收集。此时可以提供 `invitation_template`，或者让程序通过 venue group 的 `submission_name` 自动发现 invitation。实现针对 OpenReview API v2；旧 API v1 会议需要独立适配。

ACL 可设置 `options.allowed_volumes`，例如 `["2024.acl-long", "2024.acl-short", "2024.findings-acl"]`；设置为空数组表示允许事件页中的所有论文卷。每条结果保留原始卷名作为 track。旧式 ACL 编号需要显式配置范围并验证。

PMLR 新年份可配置 `options.volumes`，例如 `{"2025":267}`；没有映射会报告错误，不会静默跳过。

来源可配置 `fallbacks`，主来源失败后依次尝试备用来源。示例中的 NeurIPS 已配置[官方论文集](https://papers.nips.cc/)作为 OpenReview 的备用来源：

```json
{
  "fallbacks": [{
    "adapter": "neurips",
    "website": "https://papers.nips.cc",
    "options": {"allowed_tracks": ["main"]}
  }]
}
```

备用来源继承会议名称、年份和筛选范围，不复用主来源登录配置。跨来源记录在标题、年份、会议一致且具有共同作者、DOI 不冲突时建立身份映射，减少重复处理。主来源失败仍保留在报告中，即使备用来源成功也不会隐去错误。

403/404 不做无效的网络重试；连续三次详情访问被拒绝会停止该来源并尝试备用来源。已知失效详情 URL 可在 `options.detail_url_map` 中配置原 URL 到已核实替代 URL 的映射。没有配置替代地址的 404 仍会报告失败；ICLR 的 403 仍需可访问的数据源或有效授权，不保证自动恢复。

## 关键词规则

```json
{
  "keywords": [
    ["deepfake", "deep fake", "forgery"],
    ["detection", "localization"]
  ],
  "keyword_mode": "all",
  "exclude_keywords": ["survey"],
  "keyword_word_boundary": true
}
```

- 同一内层数组是同义词 OR；外层由 `keyword_mode` 控制 `any` / `all`。
- 上例表示：至少一个伪造相关词，并且至少一个检测／定位相关词；排除含 survey 的论文。
- 标题和摘要分别匹配，避免短语跨字段拼接误命中。
- 忽略大小写、规范化 Unicode 和连续空白；`deep-fake` 等词内连字符写法会与 `deepfake` 匹配。空格变体如 `deep fake` 可放入同义词组。
- 默认启用词边界，避免短关键词命中长单词内部；要恢复子串匹配，设置 `keyword_word_boundary=false`。
- 空关键词数组表示不过滤；空字符串或空同义词组会在配置校验时被拒绝。
- 每个来源可用 `keywords` 覆盖全局关键词。排除词和匹配模式为全局配置。
- 命中的关键词、字段及规范化文本片段写入运行报告。

## 下载、恢复与缓存

```json
{
  "download_dir": "downloads",
  "state_file": "state.json",
  "download_only": true,
  "cache_ttl_hours": 24,
  "offline": false,
  "report_file": "run_report.json"
}
```

`download_only=true` 只下载，不写入 Zotero。以后改为 `false` 并启用 Zotero，会复用 PDF 并补导入。旧字段 `dry_run` 仍可使用，但会发出弃用提示；它从来不是“不执行任何写入”的预演模式。

下载先写 `.part`，检查 HTTP 内容类型、未压缩响应的 Content-Length、PDF 文件头和结束标记，成功后原子替换最终文件。中断或无效响应不会留下被误认为成功的最终文件。现有文件也会检查，损坏文件重新下载。这些检查不能替代完整 PDF 解析器，不能保证每一页内容都可读。

状态改用 SQLite 事务。如果配置仍是 `state.json`，实际数据库为同目录 `state.sqlite3`，首次运行自动导入旧 JSON，旧文件保持原样；也可以直接配置 `.sqlite3` 路径。下载状态和 Zotero 各阶段分别保存。旧的已导入条目会先在当前 Zotero 库核对身份，再迁移其状态。

每解析成功一篇就保存元数据，包括未命中的论文；列表页、详情页和 API 响应也会缓存。完整且无解析错误的来源年份另存完整快照。修改关键词后可重新筛选缓存，提前停止后重跑可复用已有页面与摘要。默认缓存有效期 24 小时；`cache_ttl_hours=0` 强制重新抓取。缓存键包含来源、年份及适配器选项，修改范围不会误用旧缓存；解析失败的页面会移除，避免坏页面长期污染缓存。

缓存恢复不等于持久化扫描游标：每轮仍从候选顺序开始，缓存记录也占 `max_papers`。连续使用很小的扫描上限可能反复筛选同一批缓存；需要扩大扫描范围时应提高该上限。

`offline=true` 只读取已缓存元数据，不访问论文站点；没有完整快照时可读取部分缓存，报告标明不完整。命中论文若缺少有效的本地 PDF，则记录失败。需要写入 Zotero 时仍会访问本机 Zotero。只离线重筛而不下载或导入，设置 `offline=true` 和 `metadata_only=true`。

目前单进程轮换来源、串行发请求。不要同时启动多个进程处理同一数据库／下载目录；数据库事务不等于下载和远程导入的分布式任务锁。

## Zotero

启用设置中的“允许本机其他应用程序与 Zotero 通信”。本地写入集成针对 **Zotero 10+**：程序先取得 `Zotero-Server-ID`，写入遇到未授权时发起本地授权请求，由 Zotero 显示授权对话框。旧版本缺少写入能力时会明确报错，并继续独立的论文下载。

```json
{
  "zotero": {
    "enabled": true,
    "base_url": "http://127.0.0.1:23119",
    "api_key": "${ZOTERO_LOCAL_API_KEY}",
    "user_or_group": "users/0",
    "root_collection": "TFD",
    "create_subcollections": true,
    "fallback_to_root": true
  }
}
```

`api_key` 可留空，运行时授权。此处必须是本地授权密钥，不能使用 zotero.org Web API 密钥。程序只在当前进程内保留新授权密钥，不把它写入状态或日志。只允许一次的授权可能需要在后续写入时重新确认。

如果已有本地授权密钥，可通过环境变量 `ZOTERO_LOCAL_API_KEY` 提供；`${变量名}` 会在读取配置时展开，未设置的变量展开为空字符串。程序不会自动读取 `.env` 文件。真实密钥只保存在本地，公开示例配置保留环境变量占位符。

`root_collection` 是用户指定的实际 Zotero 集合名称。示例使用 `TFD`，可以改为自己的集合；不要仅为了匹配项目或目录名称而改动现有分类。

条目和附件的预分配 key 会在发送写请求前保存。即使服务器已创建条目但响应丢失，重跑也会按原 key 核对；上传失败后复用原条目和附件。导入状态按 Zotero 实例及文献库隔离。分类列表支持分页，子分类失败可按配置回退到根分类。

官方接口依据：[Zotero Local API](https://www.zotero.org/support/dev/web_api/v3/local_api)、[Write Requests](https://www.zotero.org/support/dev/web_api/v3/write_requests)。程序不会直接修改 Zotero 数据库。

## 接入期刊

`generic_html` 支持期刊元数据和 Zotero `journalArticle`。下面是**需要按目标网站修改的配置模板**，不是已验证的出版商适配器：

```json
{
  "name": "MyJournal",
  "website": "https://journal.example.org",
  "adapter": "generic_html",
  "years": [2025],
  "options": {
    "item_type": "journalArticle",
    "detail_pages": true,
    "track": "journal",
    "year_url_template": "https://journal.example.org/archive/{year}",
    "selectors": {
      "item": ".article",
      "title": "h2 a",
      "landing": "h2 a",
      "next": "a.next"
    }
  }
}
```

详情页读取 `citation_title`、`citation_author`、`citation_doi`、`citation_pdf_url`、`citation_volume`、`citation_issue`、`citation_issn`、`citation_publication_date` 等元数据。摘要支持常见容器或 `citation_abstract`。没有可用 PDF 链接时会记录失败；不绕过登录或付费墙，也尚未内置 IEEE／ACM 等出版商专用接口。

不使用详情页时，必须提供 `item`、`title`、`pdf` 选择器，`abstract`、`landing` 和 `next` 可选。选择器语法会在启动时校验。

## 报告与测试

`run_report.json` 包含：

- 总体 scanned / seen / matched / attempted / downloaded / reused / imported / skipped / failed，以及本次限额、停止原因和 elapsed_seconds。
- 每来源、每年份的扫描数、发现数、命中数、下载数、导入数、失败数、摘要缺失率，以及 complete、limited、cache_hits、adapters_tried、fallback_used。
- 失败 URL 和原因，命中词及其文本片段。

`matched` 包含已经导入的匹配论文；`attempted` 只计本轮实际进入处理的去重论文数（包含失败）；`downloaded` 只计本轮新下载，`reused` 计复用的有效本地 PDF。失败数量按失败事件计，分类初始化失败单独列在失败清单中。

`scanned` 包含详情读取失败的候选，`seen` 只包含成功读取的去重元数据。`stop_reason` 区分 `completed`、`max_matches_reached`、`max_papers_reached`、`max_seconds_reached` 和 `interrupted`。`complete=false` 表示尚未确认扫描完整，不能把有限试跑的结果当作来源全年总量。

```powershell
python -m pip install -e ".[test]"
python -m pytest -q
```

回归测试使用模拟页面和网络响应，覆盖解析、录用过滤、分页、下载中断、配置校验、状态迁移、缓存、Zotero 授权和失败恢复；不要求真实 Zotero，也不会往用户文献库写测试条目。

测试通过说明这些模拟场景下的行为符合预期，不保证真实网站当前可访问或页面结构未变化。真实抓取验证应使用独立配置、下载目录、数据库和报告路径，避免覆盖已有任务。

## 项目结构

```text
paper_crawler/
  __main__.py           # python -m paper_crawler 入口
  cli.py                # 参数解析与退出码
  config.py             # 配置校验、环境变量展开与路径解析
  pipeline.py           # 筛选、下载与 Zotero 导入流程
  schedule.py           # 会议日期与来源年份调度
  adapters/             # 来源解析适配器
  keywords.py           # 标题与摘要关键词匹配
  http_client.py        # HTTP 请求与页面缓存
  rate_limit.py         # 限速与重试等待
  budget.py             # 处理数量、扫描数量和时间预算
  storage.py            # SQLite 状态与缓存
  zotero.py             # Zotero 本地 API 集成
tests/                  # 模拟响应回归测试
config.example.json     # 可公开的配置模板
pyproject.toml          # 依赖、版本及命令入口
```

## 打包与发布

在已选定的 Python 环境、仓库根目录执行：

```powershell
python -m pip install build
python -m build
```

构建产物写入 `dist/`：wheel（`.whl`）用于安装，源码分发包（`.tar.gz`）用于源码分发。版本来自 `pyproject.toml`。发布前应检查两个包的内容，并在独立虚拟环境安装 wheel，核对两种命令入口的 `--help`；上述命令是构建步骤，不表示安装包已经验证通过。

用户配置是外部 JSON 文件。不要假定 wheel 内含 `config.example.json`；应从对应版本的源码仓库取得模板，或在 Release 中单独附上模板。源码分发包是否包含模板也需要检查，必要时补充打包清单。

首次提交建议明确指定 `paper_crawler/`、`tests/`、`README.md`、`pyproject.toml`、`config.example.json`、`.gitignore`，以及选定后的 `LICENSE`。发布前补齐许可证，明确使用和分发条件。

`.gitignore` 排除本地配置、下载目录、`runs/`、`verification/`、数据库、日志、虚拟环境及构建产物。当前工作区的 `AGENTS.md` 和历史任务日志也保留在本地。自定义输出路径可能不在这些规则内，提交前使用 `git status --short` 和 `git diff --cached` 核对内容；忽略规则不会自动移除已跟踪文件。

GitHub 仓库保存源码与公共配置模板，Release 可附 wheel、源码分发包及配置模板。下载论文与个人运行数据保留在本地。
