# 飞书知识库 · Feishu Knowledge Base

把飞书群里的讨论、附件和实践经验，整理成**有来源、可追溯、能持续增量更新**的本地知识库，并通过「知识星云」三维星图检索、阅读和沉淀笔记。

这是一个从**爬取**到**部署**的完整链路：

```text
飞书群聊 / 附件 / 回复
        │  ① 采集  exporter/
        │     lark-cli 分页导出 + 附件下载 + 话题补全
        ▼
本地消息快照（JSON / JSONL，冻结批次）
        │  ② 整理  exporter/
        │     清洗脱敏 + 候选提取 + Markdown 解析 + 本地 OCR
        │     人工复核 → 经验卡（带来源、适用范围、反例与限制）
        ▼
版本化知识数据集（version-1 JSON）
        │  ③ 转换  nebula/adapters/
        │     稳定 ID、来源核验、缺失出处拒收
        ▼
SQLite 数据库（来源 + 知识 + 个人笔记 + 修订历史）
        │  ④ 部署  nebula/
        │     本机 HTTP 服务（仅 127.0.0.1）
        ▼
三维星云 · 中文全文搜索 · 经验卡 · 来源回溯 · 收藏与笔记
```

## 项目组成

| 目录 | 环节 | 说明 |
| --- | --- | --- |
| [`exporter/`](exporter/) | 采集与整理 | 飞书群聊本地同步、增量导出、清洗、解析、OCR 与核验工具 |
| [`nebula/`](nebula/) | 部署与展示 | 知识星云：SQLite 知识库 + Python 本地服务 + Three.js 三维星图 |

两个子项目各自附带独立 README：

- 采集工具用法见 [`exporter/README.md`](exporter/README.md)
- 知识星云用法见 [`nebula/README.md`](nebula/README.md)

## 快速开始

**采集**（需要已安装并登录的 `lark-cli`）：

```bash
cd exporter
python devinsync_feishu_chat_local.py --setup   # 首次配置群 ID 等
python devinsync_feishu_chat_local.py           # 导出群消息与附件
```

**部署**（无需 pip、Node、MySQL 或 API Key，仅 Python 3.10+ 与支持 WebGL 的浏览器）：

```bash
cd nebula
python -m nebula serve --demo                   # 演示数据快速预览
python -m nebula serve                          # 导入自己的数据后启动
```

浏览器打开 http://127.0.0.1:8768/。

## 关键特性

- **有出处**：每一条知识都追溯到原始消息或文档，来源变化/撤回自动标记待复核。
- **可追溯**：经验卡记录适用范围、反例与限制；数据按冻结批次增量更新，旧批次与附件保持原样。
- **增量可持续**：显式基线 + 冻结边界 + 消息 ID 去重，中断可恢复，不会重复或丢失。
- **本机优先**：纯本地运行，Python 标准库 + SQLite + 随包 Three.js，不访问 CDN，无遥测、无外部模型调用。
- **个人记录保留**：收藏、学习状态、本地笔记与修订历史在重复导入后依然保留。

## 目录结构

```text
feishu-knowledge-base/
├── exporter/                  # 采集与整理工具
│   ├── devinsync_feishu_chat_local.py   # 主采集器
│   ├── incremental_export.py            # 冻结增量批次
│   ├── clean_chat.py                    # 清洗脱敏与候选提取
│   ├── parse_markdown_attachments.py    # Markdown 附件解析
│   ├── ocr_local.ps1                    # 本地 OCR
│   ├── verify_batch.py / verify_export.py / verify_increment.py
│   ├── inspect_increment.py / test_exporter.py
│   ├── export_all.cmd
│   └── README.md
└── nebula/                    # 知识星云服务与三维前端
    ├── nebula/                # 数据校验、SQLite、HTTP API、CLI
    ├── adapters/              # 离线消息 JSON 转换器
    ├── web/                   # 三维星图与阅读界面（含 Three.js）
    ├── examples/              # 最小数据格式示例
    ├── tests/ / tools/
    ├── start.cmd / start.sh / start-local.cmd / stop-local.cmd / refresh-local.cmd
    └── README.md
```

## 隐私与安全

本仓库**只包含代码与工具文档，不含任何真实数据、凭据或个人资料**。以下内容一律通过 `.gitignore` 排除、且从未进入版本库：

- `config.local.json`：目标群 ID 与本机路径
- 登录二维码与飞书授权凭据
- `exports/`：原始群聊消息与附件快照
- `cleaned/`：清洗与经验卡整理结果
- `data/`：个人 SQLite 数据库、笔记、收藏与导入回执

如自行使用，请同样保持这些本地产物不入库。飞书群聊内容、附件、目标群配置和个人笔记属于本地资料，采集与导入均在本机完成，本工具不会自动上传任何数据。

## 许可

代码与自编示例采用 [MIT License](LICENSE)。三维前端使用随包附带的 Three.js，其许可见 [`nebula/THIRD_PARTY_NOTICES.md`](nebula/THIRD_PARTY_NOTICES.md)。
