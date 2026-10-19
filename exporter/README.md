# 飞书群聊本地同步工具（Feishu Chat Local Exporter）

把飞书群的讨论、回复和附件，离线导出为可追溯的本地 JSON/JSONL 数据，并配套清洗、Markdown 解析、OCR 与一致性核验工具。它是「飞书知识库」全流程的**采集环节**，输出交给 [知识星云](../nebula/) 部署展示。

## 依赖

- Python 3.10+
- 已安装并登录的 `lark-cli`

官方开发者文档：https://open.larkoffice.com/document/tools-and-resources/development-tools/ide-with-commands

快速上手指南：

1. 安装工具

推荐使用 npm 进行全局安装：

```bash
npm install -g @larksuite/cli
```

（注：如果安装后提示 Command not found，请确保 npm 的全局安装目录已添加到系统的 PATH 环境变量中。）

2. 配置与授权

安装完成后，需要进行初始化配置和登录授权：

```bash
# 初始化配置（交互式引导，支持一键创建应用）
lark-cli config init

# 登录授权（--recommend 会自动选择常用权限，免去手动勾选的麻烦）
lark-cli auth login --recommend
```

当前 `lark-cli` 登录账号需要有权限读取目标群聊消息和下载资源。

## 首次运行

```bash
python devinsync_feishu_chat_local.py --setup
```

终端会提示输入：

- 飞书群 `chat_id`（输入时不会显示）

获取飞书群聊的 chat_id（群 ID）有多种方式：

1. 通过飞书客户端直接查看：飞书 PC 客户端（7.60 及以上）打开目标群聊，点击右上角「...」或「设置」进入群设置，滑到底部「群 ID」字段直接复制。
2. 通过飞书开放平台 API 调试台：选择具有群能力的应用，调用「搜索对用户或机器人可见的群列表」接口，返回结果中以 `oc_` 开头的字符串即为群 ID。
3. 通过飞书 CLI：运行群组查询命令，在返回数据中查找 `chat_id` 字段。

- 输出文件夹路径
- `lark-cli` 路径或命令
- 每页消息数 `page_size`
- 每次回扫最近几页 `tail_pages`
- 是否持续轮询及轮询间隔
- 是否下载图片/文件/音频/视频附件
- 首次同步起始时间（可留空）

默认会把配置保存到脚本同目录的 `config.local.json`。这个文件可能包含群聊 ID 和本地路径，**不要提交到 Git**。

## 普通运行

```bash
python devinsync_feishu_chat_local.py
```

临时指定起始时间：

```bash
python devinsync_feishu_chat_local.py --start "2026-06-07 14:00"
```

临时覆盖输出目录：

```bash
python devinsync_feishu_chat_local.py --output-dir ./my_export
```

只同步消息、不下载附件：

```bash
python devinsync_feishu_chat_local.py --no-download-attachments
```

持续轮询：

```bash
python devinsync_feishu_chat_local.py --interval-seconds 60
```

Windows 下也可双击 `export_all.cmd` 执行全量导出。

## 输出结构

假设输出目录是 `feishu_chat_export/`：

```text
feishu_chat_export/
├── messages.jsonl          # 每条消息一行，适合脚本处理
├── messages.json           # 完整消息数组，适合人工查看
├── messages_by_id.json     # 以 message_id 为 key 的本地索引，用于去重/更新
├── mentions.jsonl          # @ 提及索引
├── attachments.jsonl       # 附件索引、下载状态、sha256、本地路径
├── message_history.jsonl   # 消息首次出现、编辑、撤回等历史事件
├── sync_state.json         # 最近一次同步状态
├── sync_log.jsonl          # 每次运行摘要
├── attachments/            # 实际下载的图片/文件/音频/视频
└── _tmp_resources/         # 临时下载目录
```

`messages.jsonl`、`messages.json`、`mentions.jsonl`、`attachments.jsonl` 每次运行会按当前本地索引重写，避免重复消息。`message_history.jsonl` 和 `sync_log.jsonl` 是追加写入。

## 增量同步逻辑

- 首次运行：如果没有指定起始时间，会让 `lark-cli` 按可用范围抓取。
- 后续运行：读取本地 `messages_by_id.json`，找到最新 `create_time`，向前回退 2 分钟后继续同步，降低漏消息风险。
- 每次运行结束前会额外按 `tail_pages` 回扫最近几页，帮助发现编辑、撤回、附件状态变化。
- 同一 `message_id` 会本地 upsert，不会重复输出。

## 独立增量批次（冻结快照）

使用 `incremental_export.py` 为每次增量创建**独立、冻结**的新批次：默认从上一批 `increment_manifest.json` 的 `end` 续接，结束时间在启动时冻结，按消息 ID 去重，旧导出不改动；并回扫已知话题以取得旧话题的新回复。

每次请给新的输出目录，不要复用已冻结的批次：

```powershell
python incremental_export.py --base "exports/increment-上一批" --output-dir "exports/increment-下一批"
```

首次运行会在新目录保存起止边界和基线哈希；中断后用同一命令恢复。`new_messages.json` 是该批新消息，`messages.json` 是去重后的合并导出；旧附件通过相对路径引用，新附件存在新批次中。不要移动或删掉被引用的旧批次。

可用 `--start`、`--end` 指定 ISO 时间；恢复时不能更换已冻结边界。用新批次的 `new_messages.json` 做清洗时，指定唯一引用前缀。自动候选只是初筛，必须结合短回复、话题上下文和附件人工核对。

## 清洗与核验工具

替换为实际批次路径，并传入所有旧批次 audit 以保留稳定引用：

```powershell
python clean_chat.py --source 新批次/new_messages.json --output 清洗目录 --source-prefix N日期-
python parse_markdown_attachments.py --export 新批次 --output 清洗目录/markdown --audit 旧清洗目录/audit.jsonl --audit 清洗目录/audit.jsonl
python verify_batch.py --export 新批次 --output 清洗目录 --old-audit 旧清洗目录/audit.jsonl
```

各工具职责：

| 脚本 | 作用 |
| --- | --- |
| `devinsync_feishu_chat_local.py` | 主采集器：分页、ID 去重、检查点、话题完整读取、附件下载 |
| `incremental_export.py` | 从旧冻结快照建立独立新批次，续采依据 |
| `clean_chat.py` | 新消息脱敏、分类、候选提取（不能替代语义复核） |
| `parse_markdown_attachments.py` | MD 全文/结构/哈希/内容去重，传入历次 audit 保留引用 |
| `ocr_local.ps1` | 本地 Windows WinRT 中文 OCR |
| `verify_batch.py` | 通用核验：沿祖先批次检查附件、生成转发重复报告 |
| `verify_export.py` | 首次全量离线核验 |
| `verify_increment.py` | 旧批次专用检查 |
| `inspect_increment.py` | 临时视图和待处理 OCR 辅助 |
| `test_exporter.py` | 回归测试 |

## 安全建议

- 不要提交 `config.local.json`、输出目录、附件目录。
- 如果要把工具发给别人，只发脚本和 README；不要附带自己的配置或导出数据。
- `chat_id` 虽然不是密码，但仍可能暴露群信息，建议当作敏感信息处理。

## 自检

```bash
python devinsync_feishu_chat_local.py --self-test
```

自检不会调用 `lark-cli`，只验证本地写入、消息 upsert 和 mentions 解析。
