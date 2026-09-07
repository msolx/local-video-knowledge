<!-- VERSIONED SNAPSHOT -->
<!-- Source Master: G:\antigravity-cli\dy\M2_DOUYIN_COMPLETE_HANDOFF.md -->
<!-- Milestone: M2 COMPLETE -->
<!-- Snapshot Purpose: Git recovery / reproducibility -->

# Milestone M2 · Douyin Automatic Ingestion MVP 最终完整交接与冷启动恢复手册

> **Authoritative Master Entry Point (唯一权威入口与主文档)**  
> **文档物理路径**：[`G:\antigravity-cli\dy\M2_DOUYIN_COMPLETE_HANDOFF.md`](file:///G:/antigravity-cli/dy/M2_DOUYIN_COMPLETE_HANDOFF.md)  
> **MASTER DOC VERSIONING STATUS**：  
> - **OUTSIDE CURRENT GIT REPOSITORY**（位于 Git 仓库 `G:\local_pc_project` 外部）  
> - **NOT VERSIONED BY local_pc_project Git**（当前不被项目 Git 跟踪版本）  
> - **未来演进建议**：未来执行正式 M2 归档 checkpoint 时，推荐在仓库内保留一份 `docs/M2_DOUYIN_COMPLETE_HANDOFF.md` 作为版本化快照（versioned snapshot），而本文件 `G:\antigravity-cli\dy\M2_DOUYIN_COMPLETE_HANDOFF.md` 继续作为顶层编排主入口（orchestration master copy）。本轮审计严格遵循规范，**禁止向仓库内复制或执行 Git Commit**。  
> 任何后续接入或恢复本项目的 AI Agent 或工程师，**仅阅读本文档即可建立完整的系统认知、运行流水线、执行验证并排查故障**，无需翻阅几十份历史开发过程记录。

---

# 1. Executive Snapshot (行政概览)

| 项目属性 | 实际值 / 状态 |
| :--- | :--- |
| **Project Name** | Personal Knowledge Pipeline / Douyin Collection Ingestion |
| **Master Handoff Path**| [`G:\antigravity-cli\dy\M2_DOUYIN_COMPLETE_HANDOFF.md`](file:///G:/antigravity-cli/dy/M2_DOUYIN_COMPLETE_HANDOFF.md) (仓库外权威主文档) |
| **Repository Root** | `G:\local_pc_project` |
| **Current Milestone** | **Milestone M2: Douyin Automatic Ingestion MVP** |
| **Milestone Status** | **COMPLETE / FINAL STOP (流水线处于安全静止停机状态)** |
| **Last Acceptance** | `DY-C10 Final Closure Audit` (2026-09-07) |
| **Collector Subsystem**| **DY-C01 ~ DY-C10: 100% DONE** |
| **Downloader Subsystem**| **DY-D01 ~ DY-D10: 100% DONE** |
| **Current User Capability** | **用户在抖音 App/Web 手动收藏视频或图集作品 $\rightarrow$ 系统全自动增量发现 $\rightarrow$ 动态调度认证 $\rightarrow$ 真实网络下载 $\rightarrow$ 规范化命名与序列校验 $\rightarrow$ 媒体有效性探针与冒烟 $\rightarrow$ 生成 Formal Local Asset** |
| **Next Milestone** | **Milestone M3: Media Knowledge Integration** |
| **Next Milestone Status** | **NOT STARTED / FROZEN (严禁擅自启动)** |

### Git & Runtime Metadata Snapshot
- **Document Generated At**: `2026-09-07T15:45:00+08:00`
- **Git Branch**: `main`
- **Git HEAD Commit**: `29ee1e97246ceeffd9454de28a331fb96bc7c6c1`
- **Working Tree State**: `DIRTY` (包含 11 个修改文件与 54 个未跟踪模块/测试文件，属于 M2 本地增量演进产物，保持现状，禁止擅自 reset 或 commit)
- **Main Python Venv**: `G:\local_pc_project\.venv` (`Python 3.12.3`)
- **Worker Python Venv**: `G:\local_pc_project\.venv-f2` (`Python 3.12.3`, `F2 0.0.1.7`)
- **Dedicated Profile**: `G:\antigravity-cli\dy\runtime\chrome-profile` (`EXISTS`)
- **Formal Archive Root**: `G:\local_pc_project\archive` (`EXISTS`)
- **Metadata Database**: `G:\local_pc_project\data\metadata.db` (`EXISTS`)
- **Downloader Database**: `G:\local_pc_project\data\downloader_state.sqlite3` (`EXISTS`)
- **Worker Lock File**: `G:\local_pc_project\data\.worker.lock` (仅运行期间创建，停机期间不存在)

> [!CAUTION]
> ### CRITICAL RECOVERY WARNING (冷启动灾备与代码恢复关键警示)
> 1. **Git 仓库未包含全部 M2 增量代码**：当前 Git 远端分支 `origin/main` 以及本地 HEAD commit（`29ee1e9`）**绝不包含** M2 阶段本地演进出的 11 个修改文件与 54 个未跟踪文件（包括 `src/collector/`, `src/downloader/` 核心实现及 `tests/test_*` 全套测试套件）。
> 2. **单纯 `git clone` 无法恢复 M2**：如果仅在全新机器执行 `git clone <remote_url>`，拉取下来的代码**缺少全部采集与下载管线**！
> 3. **完整灾备要求**：灾备与冷启动恢复**必须完整打包或持久化宿主机的物理工作树目录**（`G:\local_pc_project` 全量工作区，包括未跟踪代码、`data/metadata.db`、`data/downloader_state.sqlite3` 以及 `G:\antigravity-cli\dy\runtime\chrome-profile`），绝不可单纯依赖 Git 远程仓库。

---

# 2. Project Goal & Scope (项目目标与边界)

### 2.1 整体项目大目标 (Personal Knowledge Pipeline)
本项目的终极愿景不是做一个普通的“抖音下载器”，而是一套端到端的个人音视频与图文多模态知识提取与沉淀系统：
```
[Source Adapters] (抖音、B站、网页等)
       ↓
[Ingestion Subsystem] (自动增量监听、排队、真实下载、防封禁合规、规范归档)
       ↓
[Media / Text Processing] (音频提取、语音转写 ASR、OCR、多模态 VLM)
       ↓
[Evidence Extraction] (时间戳对齐、视觉场景切割、图文互证)
       ↓
[Knowledge Store] (原子知识条目、实体关系图谱)
       ↓
[RAG & Agent Interface] (个人知识库检索增强、Obsidian 双链笔记、对话助手)
```

### 2.2 Milestone M2 当前边界与定位
- **M2 达成边界**：当前仅完成 **Douyin Collection $\rightarrow$ Formal Local Asset**（真实收藏捕获至本地正式规范化资产归档）。
- **后半段状态**：从规范化资产到 ASR、OCR、VLM、知识抽取、RAG、Obsidian 的后半段流水线 **全部处于 NOT STARTED 状态**。
- **定位认知**：`local-video-knowledge` 仓库未来将承担 `Media Processor` 的角色，它只是整个知识体系中的一个处理引擎，不是全系统的全部。

---

# 3. Milestone Map (里程碑全景路线图)

| 里程碑编号 | 里程碑名称 | 核心目标与交付 | 当前状态 |
| :---: | :--- | :--- | :---: |
| **M1** | **Douyin Research Complete** | 浏览器环境验证、单接口协议反向工程、F2 爬虫能力摸底与可行性论证 | **DONE** |
| **M2** | **Douyin Automatic Ingestion MVP** | 增量采集器、事务 Outbox、下载 Worker、F2 隔离接入、规范化、媒体校验、正式归档 | **COMPLETE** |
| **M3** | **Media Knowledge Integration** | 将归档视频/图集对接到本地音视频处理管线（ASR、OCR、VLM 证据层接入） | **FROZEN / NOT STARTED** |
| **M4** | **Unified Knowledge Model** | 统一的多源事实提取模型、结构化知识单元与证据绑定 | **FROZEN / NOT STARTED** |
| **M5** | **Personal RAG MVP** | 向量检索与混合图检索、上下文构建与本地模型推理集成 | **FROZEN / NOT STARTED** |
| **M6** | **Obsidian Integration** | 自动同步 Markdown 双链笔记库、元数据面板与资产嵌入 | **FROZEN / NOT STARTED** |
| **M7** | **Multi-source Knowledge System** | 扩展 Bilibili、YouTube、微信公众号等多数据源，构建跨平台知识网络 | **FROZEN / NOT STARTED** |

> [!IMPORTANT]
> 仓库内可能存在早期 POC 遗留的旧代码（如 `src/knowledge/` 或 `src/render.py`），这绝不代表 M3+ 已经启动。当前 M2 已经严格停机，严禁跨入 M3。

---

# 4. M2 Definition of Done (验收准则达成对照)

Milestone M2 核心验收标准包含 11 个原子链路环节，全部在 DY-C10 中完成 100% 验证：

1. **真实账号收藏操作**：用户在抖音手机客户端或网页端真实点击“收藏”（零预注入 content_id）。
2. **自动增量发现**：Collector 服务以 `cursor="0"` 自动发现最新收藏条目，准确提取作品元数据。
3. **认证状态防护**：探测与识别当前 Profile 认证状态（`AUTH_VALID` / `AUTH_CHALLENGE` / `AUTH_REQUIRED` / `AUTH_UNCERTAIN`），支持滑块验证恢复。
4. **增量水位推进**：成功记录并推进 `incremental_head_watermark_cursor`，识别数据库已知项并实现安全边界停机。
5. **规范化变换 (Canonical Transform)**：将平台原始响应转换为平台无关的 `CanonicalItem` 契约结构。
6. **事务 Outbox 排队**：在 Collector 事务内写入 `download_outbox`（`PENDING`, `NEW_COLLECTION_ITEM`）。
7. **Downloader Worker 领单**：`OutboxConsumerBridge` 批量调度，原子跃迁至 `DISPATCHED`，写入持久化 JobStore（`READY`）。
8. **F2 真实网络下载**：Worker 在隔离沙箱内调度 F2 引擎，动态获取真实高清媒体流并安全落盘。
9. **规范化资产处理**：`ProductionAssetNormalizer` 剔除临时碎片，图集生成严格 `1..N` 连续文件序列。
10. **多步媒体有效性校验**：`ProductionMediaValidator` 执行 ffprobe 容器探测与音视频/图像解码冒烟验证。
11. **原子物理归档推广**：`ProductionArchivePromoter` 将资产推入正式目录，生成强校验 `asset_manifest.json`，`verify_archived_asset()` 校验为 `valid=True`。

> [!NOTE]
> Docker/NAS 部署、24x7 常驻后台化、RAG 向量检索、Obsidian 笔记同步、ASR/OCR/VLM 模型推理等均不属于 M2 的 Definition of Done。

---

# 5. Vikunja Task Matrix (已交付任务权威代码索引矩阵)

所有任务已在 Vikunja 项目管理系统中完成流转归档（Project 29: `E3 · Douyin Collection Ingestion`）。以下表格链接经过真实文件系统物理核验，指向生产规范代码：

| 任务 ID | 任务代号 | 任务名称 | 最终状态 | 交付核心产物 | 对应规范生产源码 (Canonical Production Code) | 对应物理测试套件 (Physical Test Suite) | 权威历史报告 |
| :---: | :---: | :--- | :---: | :--- | :--- | :--- | :--- |
| #35 | **DY-C01** | Collector Service Skeleton | **DONE** | CLI 骨架与配置系统 | [`src/collector/cli.py`](file:///G:/local_pc_project/src/collector/cli.py) | [`tests/test_collector_skeleton.py`](file:///G:/local_pc_project/tests/test_collector_skeleton.py) | [`DY-C01_collector_skeleton.md`](file:///G:/antigravity-cli/dy/DY-C01_collector_skeleton.md) |
| #36 | **DY-C02** | Browser Runtime Provider | **DONE** | Chrome CDP 挂载与连接 | [`src/collector/douyin/browser_runtime.py`](file:///G:/local_pc_project/src/collector/douyin/browser_runtime.py) | [`tests/test_browser_runtime.py`](file:///G:/local_pc_project/tests/test_browser_runtime.py) | [`DY-C02_browser_runtime.md`](file:///G:/antigravity-cli/dy/DY-C02_browser_runtime.md) |
| #37 | **DY-C03** | Auth State Detector | **DONE** | 鉴权双层探针与 Account Scope | [`src/collector/douyin/auth_state.py`](file:///G:/local_pc_project/src/collector/douyin/auth_state.py) | [`tests/test_douyin_auth_state.py`](file:///G:/local_pc_project/tests/test_douyin_auth_state.py) | [`DY-C03_auth_state_integration.md`](file:///G:/antigravity-cli/dy/DY-C03_auth_state_integration.md) |
| #38 | **DY-C04** | Listcollection Client | **DONE** | 浏览器上下文 Web 接口代理 | [`src/collector/douyin/source_client.py`](file:///G:/local_pc_project/src/collector/douyin/source_client.py) | [`tests/test_douyin_source_client.py`](file:///G:/local_pc_project/tests/test_douyin_source_client.py) | [`DY-C04_listcollection_client.md`](file:///G:/antigravity-cli/dy/DY-C04_listcollection_client.md) |
| #39 | **DY-C05** | Incremental Sync Engine | **DONE** | 增量水位游标推进与停机 | [`src/collector/douyin/sync_engine.py`](file:///G:/local_pc_project/src/collector/douyin/sync_engine.py) | [`tests/test_incremental_sync_engine.py`](file:///G:/local_pc_project/tests/test_incremental_sync_engine.py) | [`DY-C05_incremental_sync_engine.md`](file:///G:/antigravity-cli/dy/DY-C05_incremental_sync_engine.md) |
| #40 | **DY-C06** | Raw Archiver | **DONE** | 原始响应不可变持久化 | [`src/collector/raw_archive.py`](file:///G:/local_pc_project/src/collector/raw_archive.py) | [`tests/test_raw_archiver.py`](file:///G:/local_pc_project/tests/test_raw_archiver.py) | [`DY-C06_raw_response_archiver.md`](file:///G:/antigravity-cli/dy/DY-C06_raw_response_archiver.md) |
| #41 | **DY-C07** | Canonical Transformer | **DONE** | 结构化 CanonicalItem 契约 | [`src/collector/douyin/transform.py`](file:///G:/local_pc_project/src/collector/douyin/transform.py) | [`tests/test_douyin_transform.py`](file:///G:/local_pc_project/tests/test_douyin_transform.py) | [`DY-C07_canonical_transform.md`](file:///G:/antigravity-cli/dy/DY-C07_canonical_transform.md) |
| #42 | **DY-C08** | Metadata Persistence | **DONE** | SQLite metadata.db 事务仓储 | [`src/collector/repository.py`](file:///G:/local_pc_project/src/collector/repository.py) | [`tests/test_metadata_repository.py`](file:///G:/local_pc_project/tests/test_metadata_repository.py) | [`DY-C08_metadata_persistence.md`](file:///G:/antigravity-cli/dy/DY-C08_metadata_persistence.md) |
| #43 | **DY-C09** | Download Queue Producer | **DONE** | 事务 Outbox 解耦引擎 | [`src/collector/download_queue.py`](file:///G:/local_pc_project/src/collector/download_queue.py) | [`tests/test_download_queue.py`](file:///G:/local_pc_project/tests/test_download_queue.py) | [`DY-C09_download_queue_producer.md`](file:///G:/antigravity-cli/dy/DY-C09_download_queue_producer.md) |
| #44 | **DY-C10** | Collector Final E2E Suite | **DONE** | 真实手动收藏全链路验收 | [`tests/test_collector_downloader_e2e.py`](file:///G:/local_pc_project/tests/test_collector_downloader_e2e.py) | [`tests/test_collector_downloader_e2e.py`](file:///G:/local_pc_project/tests/test_collector_downloader_e2e.py) | [`DY-C10_collector_downloader_e2e.md`](file:///G:/antigravity-cli/dy/DY-C10_collector_downloader_e2e.md) |
| #55 | **DY-D01** | Safe Downloader Refactor | **DONE** | 8 阶段下载状态机执行器 | 实现: [`src/downloader/safe_downloader.py`](file:///G:/local_pc_project/src/downloader/safe_downloader.py)<br>门面: [`src/downloader/__init__.py`](file:///G:/local_pc_project/src/downloader/__init__.py) | [`tests/test_safe_downloader.py`](file:///G:/local_pc_project/tests/test_safe_downloader.py) | [`DY-D01_safe_downloader_refactor.md`](file:///G:/antigravity-cli/dy/DY-D01_safe_downloader_refactor.md) |
| #56 | **DY-D02** | Credential Provider | **DONE** | 进程内凭证快照隔离注入 | [`src/downloader/credentials.py`](file:///G:/local_pc_project/src/downloader/credentials.py) | [`tests/test_credentials.py`](file:///G:/local_pc_project/tests/test_credentials.py) | [`DY-D02_credential_provider.md`](file:///G:/antigravity-cli/dy/DY-D02_credential_provider.md) |
| #57 | **DY-D03** | F2 Backend Adapter | **DONE** | F2 进程内适配与沙箱重定向 | [`src/downloader/f2_backend.py`](file:///G:/local_pc_project/src/downloader/f2_backend.py) | [`tests/test_f2_backend.py`](file:///G:/local_pc_project/tests/test_f2_backend.py) | [`DY-D03_f2_backend.md`](file:///G:/antigravity-cli/dy/DY-D03_f2_backend.md) |
| #58 | **DY-D04** | Task Sandbox Provider | **DONE** | UUID 任务沙箱与 GC 回收 | [`src/downloader/sandbox.py`](file:///G:/local_pc_project/src/downloader/sandbox.py) | [`tests/test_task_sandbox.py`](file:///G:/local_pc_project/tests/test_task_sandbox.py) | [`DY-D04_task_sandbox.md`](file:///G:/antigravity-cli/dy/DY-D04_task_sandbox.md) |
| #59 | **DY-D05** | Media Validator | **DONE** | ffprobe 容器探测与解码冒烟 | [`src/downloader/validator.py`](file:///G:/local_pc_project/src/downloader/validator.py) | [`tests/test_media_validator.py`](file:///G:/local_pc_project/tests/test_media_validator.py) | [`DY-D05_media_validator.md`](file:///G:/antigravity-cli/dy/DY-D05_media_validator.md) |
| #60 | **DY-D06** | Asset Normalizer | **DONE** | 规范命名与图集连续序列 | [`src/downloader/normalizer.py`](file:///G:/local_pc_project/src/downloader/normalizer.py) | [`tests/test_asset_normalizer.py`](file:///G:/local_pc_project/tests/test_asset_normalizer.py) | [`DY-D06_asset_normalizer.md`](file:///G:/antigravity-cli/dy/DY-D06_asset_normalizer.md) |
| #61 | **DY-D07** | Atomic Archive Promoter | **DONE** | 正式归档移动与清单落盘 | [`src/downloader/promoter.py`](file:///G:/local_pc_project/src/downloader/promoter.py) | [`tests/test_archive_promoter.py`](file:///G:/local_pc_project/tests/test_archive_promoter.py) | [`DY-D07_atomic_archive_promotion.md`](file:///G:/antigravity-cli/dy/DY-D07_atomic_archive_promotion.md) |
| #62 | **DY-D08** | Error Taxonomy & Retry Policy | **DONE** | 确定性错误分类与退避策略 | [`src/downloader/retry_policy.py`](file:///G:/local_pc_project/src/downloader/retry_policy.py) | [`tests/test_retry_policy.py`](file:///G:/local_pc_project/tests/test_retry_policy.py) | [`DY-D08_retry_policy.md`](file:///G:/antigravity-cli/dy/DY-D08_retry_policy.md) |
| #63 | **DY-D09** | Content Router | **DONE** | 视频与图集确定性路由器 | 实现: [`src/downloader/router.py`](file:///G:/local_pc_project/src/downloader/router.py)<br>门面: [`src/downloader/__init__.py`](file:///G:/local_pc_project/src/downloader/__init__.py) | [`tests/test_content_router.py`](file:///G:/local_pc_project/tests/test_content_router.py) | [`DY-D09_content_router.md`](file:///G:/antigravity-cli/dy/DY-D09_content_router.md) |
| #64 | **DY-D10** | Downloader Worker Service | **DONE** | 进程排他锁与后台作业调度 | [`src/downloader/worker.py`](file:///G:/local_pc_project/src/downloader/worker.py) | [`tests/test_downloader_worker.py`](file:///G:/local_pc_project/tests/test_downloader_worker.py) | [`DY-D10_downloader_worker_e2e.md`](file:///G:/antigravity-cli/dy/DY-D10_downloader_worker_e2e.md) |

---

# 6. Overall System Architecture (系统顶层架构图)

```
[ 用户在抖音 App / Web 界面收藏作品 ]
                  │
                  ▼ (零预注入 content_id，全自动发现)
[ Chrome CDP Browser Profile ] (G:\antigravity-cli\dy\runtime\chrome-profile)
                  │
                  ▼
[ C02 BrowserRuntimeProvider ] (挂载现有 Chrome 实例)
                  │
                  ├── C03 DouyinAuthStateDetector (被动探测: AUTH_VALID / AUTH_CHALLENGE / AUTH_REQUIRED / AUTH_UNCERTAIN)
                  ├── C04 DouyinSourceClient (代理执行 POST /aweme/v1/web/aweme/listcollection/)
                  │
                  ▼
[ C05 DouyinIncrementalSyncEngine ] (增量同步引擎: cursor="0" 探测，触碰 head watermark 立即停机)
                  │
        ┌──────────┴──────────┐
        ▼                     ▼
[ C06 RawArchiver ]   [ C07 CanonicalTransformer ]
(原始 JSON 存入 raw/) (转为结构化 CanonicalItem 契约)
        │                     │
        └──────────┬──────────┘
                   ▼
[ C08 CollectorRepository ] (SQLite metadata.db 事务提交: collection_items / observations)
                   │
                   ▼
[ C09 DownloadQueueProducer ] (写入 download_outbox 表: status='PENDING', reason='NEW_COLLECTION_ITEM')
                   │
                   ▼
══════════════════════ [ 异步交付边界 / 事务解耦 ] ══════════════════════
                   │
                   ▼
[ D10 OutboxConsumerBridge ] (狭义交付轮询: download_outbox -> PENDING 跃迁为 DISPATCHED)
                   │
                   ▼
[ DownloaderJobStore ] (SQLite downloader_state.sqlite3: 创建/更新持久化作业 state='READY')
                   │
                   ▼
[ D10 DownloaderWorkerService ] (守护调度服务: 持有 data/.worker.lock 物理文件排他锁)
                   │
                   ├── D08 ProductionDownloadErrorPolicy (错误分类与智能退避重试决策)
                   │
                   ▼
[ D01 SafeDouyinDownloader ] (8 阶段执行状态机: PREFLIGHT -> SANDBOX -> DOWNLOADING -> NORMALIZING -> VALIDATING -> PROMOTING)
                   │
                   ├── D09 ProductionContentRouter (基于 content_type 确定性选择 video/image_album 策略)
                   ├── D02 DouyinCredentialProvider (安全快照隔离注入临时凭据上下文)
                   ├── D03 F2InProcessBackendAdapter (沙箱重定向抓取与现场环境还原)
                   ├── D04 ProductionTaskSandboxProvider (尝试隔离沙箱: input/work/output/logs)
                   ├── D06 ProductionAssetNormalizer (规范命名: {id}.mp4 / {id}_img_{seq}.webp)
                   ├── D05 ProductionMediaValidator (ffprobe 结构探针 + 解码冒烟强校验)
                   └── D07 ProductionArchivePromoter (原子移动至 formal 目录，生成 asset_manifest.json)
                   │
                   ▼
[ G:\local_pc_project\archive\douyin\<platform_content_id>\ ] (本地正式规范化归档资产)
                   │
                   └── verify_archived_asset() 校验 valid=True, SHA-256 强校验通过
```

---

# 7. Component Responsibilities & Boundaries (组件职责与清晰边界)

为避免组件职责交叉重叠，各模块的核心职责与严苛边界定义如下：

- **C02 BrowserRuntime**: 专属负责 Chrome 实例的启动、CDP 挂载、页面管理与优雅退出。**禁止在其中编写业务抓取逻辑**。
- **C03 AuthDetector**: 专属负责认证状态探针。契约状态严格为 `AUTH_VALID`, `AUTH_REQUIRED`, `AUTH_CHALLENGE`, `AUTH_UNCERTAIN`。**只读检测，禁止在探测时篡改 Cookie**。
- **C04 SourceClient**: 专属负责调用抖音官方 Web 收藏列表接口。**只管请求与原始数据获取，不做业务过滤**。
- **C05 SyncEngine**: **拥有增量水位语义（Incremental Watermark Semantics）的唯一所有者**。掌控探测起点（永远 `cursor="0"`）、停机水位判定与断点推进。
- **C06 RawArchiver**: 专属负责原始响应落盘与清单管理，记录完整性证据。
- **C07 Transformer**: 专属负责平台脏数据至通用 Canonical 数据结构的字段清洗与类型映射。
- **C08 MetadataRepo**: **拥有 `metadata.db` 核心元数据（`sync_state`, `sync_runs`, `collection_items`, `collection_observations`, `run_item_staging`）的唯一写权限所有者**。事务性提交与观测日志追加写入。
- **C09 DownloadQueue**: **拥有下载意图与事务 Outbox 的唯一所有者**。负责将新收藏项转化为规范的 `DownloadTask` 消息写入 `download_outbox`。
- **D10 OutboxConsumerBridge**: **仅拥有对 `download_outbox` 极窄的交付状态更新权限**（`poll_pending_outbox`, `mark_outbox_dispatched`, `mark_outbox_failed`）。**绝对禁止向 Collector 的 `sync_state`, 水位, `collection_items`, `collection_observations`, `sync_runs` 执行任何写操作！**
- **D10 WorkerService & JobStore**: **拥有 Downloader 内部执行状态与作业持久化的唯一所有者**（管理 `downloader_state.sqlite3` 中的 `download_jobs`, `download_attempts`, `scope_pauses`）。
- **ServiceLock 机制**: 由专用文件系统锁 `data/.worker.lock` 承担唯一的服务排他锁存储（记录三元组 `pid:process_create_time:instance_id`），**SQLite 内部无 service_locks 表**。
- **D01 SafeDownloader**: 专属负责单任务 8 阶段生命周期编排。作为外观门面，本身不写具体下载协议。
- **D02 CredentialProvider**: 专属负责运行时凭据快照的安全提取、脱敏封装与上下文生存期管控。**禁止将凭据持久化到硬盘或数据库**。
- **D03 F2Backend**: **仅拥有 F2 爬虫能力适配与抓取**。不拥有媒体校验，不拥有最终归档，不拥有命名规范。
- **D04 SandboxProvider**: **拥有沙箱生命周期与临时孤儿垃圾回收（GC）的唯一所有者**。
- **D05 MediaValidator**: 专属负责媒体流结构验证与解码冒烟。**不具有路由决策权，仅负责校验**。
- **D06 AssetNormalizer**: 专属负责标准命名规划、图集连续序号生成与 Windows 长路径防御。
- **D07 ArchivePromoter**: **拥有正式归档发布与原子提交的唯一所有者**。负责将沙箱成果提升至最终归档目录并落盘 `asset_manifest.json`。
- **D08 ErrorPolicy**: 专属负责基于 `DownloaderErrorCode` 契约分类体系执行纯确定性的重试/冷却/休眠/终止决策。
- **D09 ContentRouter**: **拥有内容类型路由权威（Routing Authority）的唯一所有者**。依据 `content_type` 决定具体管线。

---

# 8. Frozen Semantic Invariants & Legacy Exceptions (已冻结核心语义不变量与历史特例)

以下 30 条核心不变量经过严格集成测试与用户验收后正式固化，**未来任何 Agent 严禁随意更改**：

1. **接口语义区分**：`GET /favorite/` 代表“点赞（Likes）”，绝不是收藏；`POST /listcollection/` 才是真实的“用户收藏列表”。
2. **发布时间与收藏时间**：抖音返回的 `create_time` 是作品发布时间，绝不是用户点击收藏的时间。
3. **首次发现时间**：`first_seen_at` 是系统首次观测到该收藏项的时间戳，不等于历史真实收藏时间。
4. **游标所属归属**：分页游标 `cursor` 严格属于单次同步观测（Observation / SyncRun），绝不属于单个收藏作品实体。
5. **日常增量起始点**：日常增量探测**永远从 `cursor="0"` 开始**向下扫描，绝不能将历史游标作为下一次日常探测的起始点。
6. **回溯与增量隔离**：历史回溯游标（`backfill_checkpoint_cursor`）与增量头部水位（`incremental_head_watermark_cursor`）完全分离，互不替代。`committed_watermark_cursor` 属于旧版 v1 兼容字段。
7. **全量完备状态**：`history_complete` 表示全量历史是否已扫到尽头（`has_more == 0`），与头部增量水位语义分离。
8. **任务脱敏原则**：`DownloadTask` 契约对象中**绝对禁止包含真实凭据数据**（Cookie/Token）。
9. **交付与完成解耦**：C09 Outbox 的 `DISPATCHED` 仅代表下游 Downloader 已持久化认领任务，**绝不等于下载成功**。
10. **重试次数语义隔离**：Outbox 的投递尝试次数（`attempt_count`）与 Downloader 的物理执行重试次数（`attempts`）完全独立。
11. **逻辑与物理 ID 隔离**：逻辑任务标识 `task_id` 跨多次重试保持稳定；单次物理执行的 `execution_id`（UUID）每次尝试均动态新建。
12. **鉴权受阻非致命**：遇到人机滑块或 Cookie 过期时的 `BLOCKED_AUTH` 是等待人工介入或重刷的挂起状态，绝非 `TERMINAL_FAILED`。
13. **校验器无路由权**：D05 媒体校验器的 `AUTO` 探测能力仅作为后置校验依据，绝不是生产环境的前置内容路由依据。
14. **内容路由决定权**：Canonical Item 的 `content_type` 是 D09 内容路由的唯一生产权威。
15. **物理资产账号解耦**：正式物理资产归档目录结构**绝对不包含 `scope_id`**，实现跨账号/重收藏天然物理去重。
16. **物理唯一标识**：正式资产的物理唯一身份仅由 `(platform, platform_content_id)` 决定。
17. **账号范围语义**：`scope_id` 仅用于鉴权上下文注入、任务来源追溯与并发执行隔离。
18. **正式资产有效性定义**：目录存在 + `asset_manifest.json` 有效 + 声明的文件物理存在 + SHA-256 哈希全部吻合，四者同时满足才算有效资产。
19. **抓取成功非最终成功**：`Backend success`（F2 网络下载完成）$\neq$ `Download success`。
20. **校验成功非最终成功**：`Validate success`（ffprobe 校验通过）$\neq$ `Download success`。
21. **最终成功的唯一标志**：只有 D07 完成原子移动并成功落盘 `asset_manifest.json`，作业状态才能置为 `SUCCEEDED`。
22. **推广器不负责沙箱清理**：D07 ArchivePromoter 仅负责归档推广，绝不拥有沙箱清理权限。
23. **沙箱回收唯一权**：D04 TaskSandboxProvider 是沙箱目录生命周期与孤儿回收的唯一所有者。
24. **心跳超时非进程死亡**：心跳文件过期仅代表“Worker 实例可能卡顿或假死”，绝不能仅因心跳超时就强行夺取锁。
25. **进程锁双重保护**：只有在确认原 PID 对应进程已在操作系统层面死亡，或原 PID 已被其它进程重用（创建时间不一致）时，才允许安全抢占锁。
26. **主环境隔离禁令**：主虚拟环境（`G:\local_pc_project\.venv`）**绝对禁止安装或导入 F2 库**。
27. **专属 Worker 环境**：F2 专属运行在 `G:\local_pc_project\.venv-f2` 环境中，防止第三方依赖污染主工程。
28. **安全清理局限性认知**：Python 的内存变量清理是应用层脱敏，不是操作系统层面的密码学安全擦除。
29. **图集序列连续性**：图集所有图片必须被重命名为连续的 `001, 002, 003...` 序号，中间不允许出现空洞。
30. **零撞网原则**：在非必要场景或断网审计阶段，严禁触发任何线上抖音 API 请求或频繁拉起 Chrome 撞网。

> [!IMPORTANT]
> ### LEGACY SEMANTIC EXCEPTION (历史 Outbox 交付标记语义特例)
> - **当前 D10 规范协议**：新产生的 Outbox 记录严格遵循 `PENDING` $\rightarrow$ `Downloader durable accept (JobStore committed)` $\rightarrow$ `DISPATCHED`。即**只有在 Downloader 持久化认领成功后，才能置为 DISPATCHED**。
> - **49 条历史测试记录特例**：数据库中现存的 49 条早期记录生成于 Pre-D10 阶段（DY-C08/C09 开发与批量投递吞吐验证期），其 `DISPATCHED` 状态属于历史测试投递标记，**绝不得解释为“已由 D10 Downloader JobStore 持久化接收”**。
> - **运维与健康检查硬约束**：
>   1. 这 49 条记录**不代表数据丢失**，也**不代表 49 个当前下载任务悬挂**。
>   2. 未来的系统健康检查器（Health Checker）**绝对不允许**直接使用当前的 D10 协议不变量去反向审计这些历史测试记录并报出“任务脱节”。
>   3. **严禁**自动对这些历史记录执行 requeue、repair 或 delete 操作，除非未来有经过正式立项的专项 DB 迁移脚本。
>   4. 任何在当前规范协议下产生的新任务，仍必须 100% 严格执行“持久化接收后方可标记 DISPATCHED”。

---

# 9. Runtime Environments (运行环境实况)

| 运行时环境 | 绝对路径 | 核心组件 / 版本 | 责任归属 |
| :--- | :--- | :--- | :--- |
| **Repo Root** | `G:\local_pc_project` | Git 仓库根目录 | 全局根工作区 |
| **Main Python** | `G:\local_pc_project\.venv\Scripts\python.exe` | Python 3.12.3, pytest 8.3.4, playwright 1.55.0 | Collector / CLI / 主测试套件 |
| **Worker Python** | `G:\local_pc_project\.venv-f2\Scripts\python.exe`| Python 3.12.3, **f2 0.0.1.7** | Downloader Worker 专属执行环境 |
| **Chrome Profile**| `G:\antigravity-cli\dy\runtime\chrome-profile` | 真实 Windows Chrome 用户配置目录（含已登录 Cookies） | 认证态与 CDP 挂载专属 Profile |
| **ffmpeg Binary** | `C:\Users\Sean\AppData\Local\Microsoft\WinGet\Packages\BtbN.FFmpeg.GPL_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-N-125875-g5d4d3bdc61-win64-gpl\bin\ffmpeg.exe` | version N-125875-g5d4d3bdc61 (2026-07-31) | 媒体转码与分析工具 |
| **ffprobe Binary**| `C:\Users\Sean\AppData\Local\Microsoft\WinGet\Packages\BtbN.FFmpeg.GPL_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-N-125875-g5d4d3bdc61-win64-gpl\bin\ffprobe.exe` | version N-125875-g5d4d3bdc61 (2026-07-31) | D05 媒体流结构校验探针 |

---

# 10. Configuration Inventory (配置资产清册)

| 配置项 (Config Item) | 当前实际值 / 路径 | 默认定义文件 | 归属子系统 | 敏感性 | 说明 |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **Collector DB Path** | `G:\local_pc_project\data\metadata.db` | [`src/collector/config.py`](file:///G:/local_pc_project/src/collector/config.py) | Collector | 否 | 采集核心状态库 |
| **Downloader DB Path**| `G:\local_pc_project\data\downloader_state.sqlite3` | [`src/downloader/worker.py`](file:///G:/local_pc_project/src/downloader/worker.py) | Downloader| 否 | 作业调度与重试状态库 |
| **Raw Archive Root** | `G:\local_pc_project\data\raw` | [`src/collector/config.py`](file:///G:/local_pc_project/src/collector/config.py) | Collector | 否 | 原始 JSON 响应证据目录 |
| **Sandbox Root** | `G:\local_pc_project\data\sandbox` | [`src/downloader/sandbox.py`](file:///G:/local_pc_project/src/downloader/sandbox.py) | Downloader| 否 | 任务级临时沙箱存放根路径 |
| **Formal Archive Root**| `G:\local_pc_project\archive` | [`src/downloader/worker.py`](file:///G:/local_pc_project/src/downloader/worker.py) | Downloader| 否 | 正式落盘物理媒体资产归档目录 |
| **Chrome Profile Path**| `G:\antigravity-cli\dy\runtime\chrome-profile` | 命令行参数 / 专用配置 | 全局认证 | **极高密 / 运行时凭据资产** | 包含用户真实登录会话，DPAPI 加密，**绝不可入 Git** |
| **Worker Service Lock**| `G:\local_pc_project\data\.worker.lock` | [`src/downloader/service_lock.py`](file:///G:/local_pc_project/src/downloader/service_lock.py) | Downloader| 否 | 物理文件排他锁（PID:StartTime:UUID） |
| **Poll Interval** | `2.0` 秒 | [`src/downloader/worker.py`](file:///G:/local_pc_project/src/downloader/worker.py) | Worker | 否 | Worker 主循环轮询间隔 |
| **Auth Recheck Interval**| `60.0` 秒 | [`src/downloader/worker.py`](file:///G:/local_pc_project/src/downloader/worker.py) | Worker | 否 | 认证状态心跳复检最小周期 |
| **SQLite Busy Timeout**| `30000` 毫秒 (30s) | 各仓储层 SQLite 连接代码 | 全局 | 否 | 并发访问冲突退避超时 |
| **Windows Path Budget**| `240` 字符 | [`src/downloader/normalizer.py`](file:///G:/local_pc_project/src/downloader/normalizer.py) | Downloader| 否 | MAX_PATH 防御阈值 |
| **Session Cookies** | **`[REDACTED / NOT STORED IN CONFIG]`** | 仅运行时从 Chrome Profile 内存读取 | 全局认证 | **极高密**| **禁止写入任何配置文件** |

---

# 11. Data & Database Inventory (数据库架构与数据权属)

系统设计遵循严格的读写分离与单一所有权原则，采用双 SQLite 数据库与专用文件锁系统化物理解耦：

### 11.1 Collector Database: `data/metadata.db`
- **所有者 (Owner)**：Collector 子系统专属写入。Downloader 仅由 `OutboxConsumerBridge` 只读轮询 `download_outbox` 并回写任务投递状态（`PENDING` $\rightarrow$ `DISPATCHED` 或投递失败）；Downloader **绝对不可写入** Collector 的任何业务表（`sync_state`, `sync_runs`, `collection_items`, `collection_observations`, `run_item_staging`）。
- **Schema 迁移版本**：`v3` (由 `schema_migrations` 自动管理)
- **核心数据表清单**：
  1. `schema_migrations`: 记录迁移版本与执行时间（v1: 基础骨架, v2: 增量与回溯分离, v3: 事务 Outbox 队列）。
  2. `sync_state`: 当前增量头部水位与历史完备性覆盖记录（`(scope_id, platform)` 主键）。
     - `incremental_head_watermark_cursor`: 当前生效的增量头部水位游标（TEXT）。
     - `committed_watermark_cursor`: v1 遗留兼容游标字段。
     - `history_complete`: 历史全量回溯是否已完备（0 或 1）。
     - `backfill_checkpoint_cursor`: 历史回溯断点游标。
  3. `sync_runs`: 同步批次生命周期记录（状态包含 `RUNNING`, `SUCCESS`, `FAILED`）。
  4. `collection_items`: 实体主表（`(scope_id, platform, platform_content_id)` 主键，记录 `active`, `published_at`, `canonical_json` 等）。
  5. `collection_observations`: 观测事实流水表（记录每次同步观测到该作品时的页面位置、时间与 RawArchive 关联哈希）。
  6. `run_item_staging`: 批次内临时暂存表（保障网络分页拉取失败时 100% 回滚，不污染主表）。
  7. `download_outbox`: 事务性下载意图队列表（`outbox_id`, `task_id`, `payload_json`, `status`, `created_at`, `available_at`, `dispatched_at` 等）。

### 11.2 Downloader Database: `data/downloader_state.sqlite3`
- **所有者 (Owner)**：Downloader Worker 与 `DownloaderJobStore` 专属管理。
- **SQLite 实际数据表清单 (经过只读 sqlite_master 物理核验)**：
  1. `download_jobs`: 持久化下载作业主表（`task_id` 主键，状态流转: `READY` $\rightarrow$ `RUNNING` $\rightarrow$ `SUCCEEDED` / `RETRY_WAIT` / `BLOCKED_AUTH` / `TERMINAL_FAILED`，字段 `claimed_by` 记录领单 Worker 身份）。
  2. `download_attempts`: 单次物理执行尝试的审计历史表（记录每次尝试的 `attempt_number`, `execution_id`, 耗时, 错误码与脱敏后的执行事实）。
  3. `scope_pauses`: 账号作用域熔断/休眠记录表（记录 429 频率限制或滑块挑战时的解禁时间戳）。
- **ServiceLock 权威存储澄清**：`downloader_state.sqlite3` 内部**不存在 `service_locks` 数据表**。Worker 进程排他服务锁权威存储为物理文件 `data/.worker.lock`，由 `DownloaderServiceLock` 统一管理（记录三元组 `pid:process_create_time:instance_id` JSON）。

---

# 12. Current Collector State Snapshot (采集器当前状态实况)

从 `G:\local_pc_project\data\metadata.db` 现场只读查询的主账号 `douyin:dyacct_e34e68ae845897f6` 真实状态如下：
```json
{
  "scope_id": "douyin:dyacct_e34e68ae845897f6",
  "platform": "douyin",
  "incremental_head_watermark_cursor": "1788536236926781",
  "committed_watermark_cursor": "1788536236926781",
  "history_complete": 1,
  "backfill_checkpoint_cursor": null,
  "last_successful_sync_run_id": "sync_douyin_coll_20260906_152841_38ee94",
  "head_anchor_content_id": null,
  "updated_at": "2026-09-06T15:28:42.692186+00:00"
}
```
- **核心实体数统计**：`collection_items` 共记录 **51 条有效收藏作品**。
- **最新同步批次状态**：`sync_runs` 最新记录为 `sync_douyin_coll_20260906_152841_38ee94`，状态为 `SUCCESS`，停止原因为 `watermark_reached`，候选水位为 `"1788536236926781"`。

---

# 13. Current Outbox & Job Store Reconciliation (51 条 Outbox 与 2 个 Jobs 对账释疑)

在当前数据库审计中，存在表面数量差异，特此提供不可辩驳的执行现场对账证据：

### 13.1 数据现状对比
- `metadata.db` 中的 `download_outbox` 表：**共 51 条记录，状态全部为 `DISPATCHED`**。
- `downloader_state.sqlite3` 中的 `download_jobs` 表：**共 2 条记录，状态全部为 `SUCCEEDED`**。

### 13.2 批次溯源审计详情 (By `source_sync_run_id`)
通过只读 SQL 分组分析 `download_outbox` 的产生与交付批次：

| `source_sync_run_id` | 条数 | 创建时间 (UTC) | 交付时间 (UTC) | 对应业务场景说明 |
| :--- | :---: | :--- | :--- | :--- |
| `sync_douyin_coll_20260906_150950_2ae2b2` | 47 | 15:09:57 | 15:11:20 | C08/C09 早期全量收藏基线同步，由 Outbox 批量投递吞吐测试标记为 `DISPATCHED` (Pre-D10 历史测试遗留标记) |
| `sync_douyin_coll_20260906_151440_8d0f2a` | 2 | 15:14:42 | 15:16:22 | 包含真实用户新增收藏的测试视频 `7681603850364521734` 及历史项目 |
| `sync_douyin_coll_20260906_152042_755109` | 1 | 15:20:44 | 15:20:44 | 包含真实用户新增收藏的测试图集 `7682038498466993905` |
| `sync_douyin_coll_20260906_152641_fbee9a` | 1 | 15:26:44 | 15:27:59 | C10 重复同步幂等性验证测试批次 |
| **总计** | **51** | | | |

### 13.3 核心原因结论与历史特例
1. **49 条属于 Pre-D10 / 测试期遗留状态（LEGACY PRE-D10 DELIVERY MARKERS）**：在开发 DY-C08 与 DY-C09 事务队列阶段，以及进行 `OutboxConsumerBridge` 批量交付吞吐验证时，49 条历史记录被投递测试标记为 `DISPATCHED`，但当时尚未连通完整的 D10 Worker 真实物理下载执行状态机。
2. **2 条属于 C10 真实端到端闭环资产**：在正式的 DY-C10 端到端全链路验收中，用户真实收藏的两个作品：
   - 视频：`7681603850364521734` (`dl_douyin_7681603850364521734_aca45416a2adb423`)
   - 图集：`7682038498466993905` (`dl_douyin_7682038498466993905_e0b163ebbe775c40`)  
   被完整接入 Downloader Worker 守护管道，真实拉取网络流、完成容器探测、通过解码冒烟并原子提升为正式物理归档，状态置为 `SUCCEEDED`。
3. **符合 Legacy Semantic Exception**：这 49 条记录不代表数据丢失，亦不代表任务悬挂。禁止自动对其重试或删除。

---

# 14. Current Downloader State Snapshot (下载器当前状态实况)

从 `G:\local_pc_project\data\downloader_state.sqlite3` 现场只读查询的作业与锁状态：
- **`data/.worker.lock` 物理锁文件**：当前不存在（无活跃持锁进程，流水线处于绝对安全的静止停机状态）。
- **`scope_pauses` 表**：当前为 `0` 行（无任何被熔断或等待冷却的作用域）。
- **`download_attempts` 表**：记录了真实执行的尝试事实。
- **`download_jobs` 表**：共 `2` 行，状态全部为 **`SUCCEEDED`**：
  1. `dl_douyin_7681603850364521734_aca45416a2adb423`: content_type=`video`, state=`SUCCEEDED`
  2. `dl_douyin_7682038498466993905_e0b163ebbe775c40`: content_type=`image_album`, state=`SUCCEEDED`

---

# 15. Confirmed Formal Assets (物理核验证实正式归档资产)

当前磁盘上存在两个经过 100% 物理与探针核验通过的本地正式物理资产（Formal Local Assets）：

### 15.1 真实视频资产 (Video E2E Asset - 物理核验 Ground Truth)
- **目录**：`G:\local_pc_project\archive\douyin\7681603850364521734`
- **媒体文件**：`7681603850364521734.mp4`
- **文件物理大小 (Byte Size)**：`173,847,684` 字节 (~165.8 MB)
- **文件 SHA-256**：`3959a0561c58cea93b2d9093f66bf5b306afa888b914657140aa6c2b01bee7ad`
- **视频流编码 (Video Codec)**：`hevc` (HEVC / H.265 Main 10)
- **分辨率 (Resolution)**：`2160x3840` (4K 竖屏)
- **媒体时长 (Duration)**：`371.71` 秒 (371.706009s, ~6.19 分钟)
- **音频流编码 (Audio Codec)**：`aac` (44.1 kHz, 立体声)
- **清单路径**：`G:\local_pc_project\archive\douyin\7681603850364521734\asset_manifest.json`
- **清单内记录**：`byte_size=173847684`, `sha256=3959a0561c58cea93b2d9093f66bf5b306afa888b914657140aa6c2b01bee7ad` (100% 吻合)
- **标准探针校验**：`verify_archived_asset()` $\rightarrow$ **`valid=True, error=None`**

### 15.2 真实图集资产 (Image Album E2E Asset & BGM 对账)
- **目录**：`G:\local_pc_project\archive\douyin\7682038498466993905`
- **包含媒体文件**：
  1. `7682038498466993905_img_001.webp` (32,690 bytes, Seq 1, SHA-256: `c42d50171f51030ad52b761f93d6017da8f674c10a01a7b779ed925e21971d4d`)
  2. `7682038498466993905_img_002.webp` (87,294 bytes, Seq 2, SHA-256: `7d94cd948323f7b8d42a0898e10d89d060fe3d952b880206772c1459629eea64`)
  3. `7682038498466993905_img_003.webp` (112,706 bytes, Seq 3, SHA-256: `de6ae206918f87fa42aa99e76ca12d137fd57869c2ece3a4a7ff60dcbbe144ea`)
- **角色 (Role)**：3 项 `ArtifactRole.ALBUM_IMAGE`
- **连续序列验证**：严格连续 `[1, 2, 3]` 无缺漏
- **BGM 音乐流对账说明 (Optional BGM Contract)**：
  - 抖音原始响应中确实声明了背景音乐音频（`has_audio=True`, `audio_title="[REDACTED_TITLE]"`, `play_urls=[REDACTED_MEDIA_URL]`）。
  - 在 D09 内容路由与 D06/D07 资产规范契约中，图集作品的核心归档主体为**连续规范化图片序列**。
  - D03 F2 抓取后端成功下载了图集全部 3 张高清图片；正式归档目录持久化了经过严格校验的 3 张 WebP 图片，完全符合当前“图集以图像为主体，背景音乐流属于可选附加流、不强制打包进主图集目录”的规范定义。
- **清单路径**：`G:\local_pc_project\archive\douyin\7682038498466993905\asset_manifest.json`
- **校验状态**：`verify_archived_asset()` $\rightarrow$ **`valid=True, error=None`**

---

# 16. Historical Test Assets (历史开发测试样本说明)

为了防止未来恢复时误将开发阶段的夹具混为正式资产，特此区分：
- `6611417973221494020`: `G:\local_pc_project\video_6611417973221494020_detail.json`（早期 D03/D05 开发用的离线视频元数据夹具）。
- `7169622286633274635`: `G:\local_pc_project\album_7169622286633274635_detail.json`（早期 D03/D06 开发用的离线图集元数据夹具）。
- `7681627509745519918`: D10 真实网络压测临时作品，已完成历史使命并清理，磁盘当前状态为 `not retained / temporary test artifact`。

---

# 17. Authentication Architecture (认证与会话凭据架构)

```
[ G:\antigravity-cli\dy\runtime\chrome-profile ]
                      │ (包含 Windows DPAPI 加密的 Cookies 文件)
                      ▼
[ DouyinBrowserRuntimeProvider ] (通过 CDP 挂载无头/有头 Chrome 进程)
                      │
                      ▼
[ DouyinAuthStateDetector ] (提取当前激活上下文，计算 account_scope_id)
                      │
                      ▼
[ DouyinCredentialProvider ] (生成 BrowserRuntimeCredentialSource 快照)
                      │
                      ▼
[ CredentialContext (ContextManager) ] (短生存期临时上下文注入)
                      │
                      ▼
[ F2InProcessBackendAdapter ] (在单次网络抓取中动态传递 Cookie 头)
                      │
                      ▼
[ Context Exit: 内存引用即时清空 / 脱敏 ]
```

- **Scope ID 派生算法**：由账号 UID 的 SHA-256 哈希前缀派生，形式为 `douyin:dyacct_<short_hash>`，确保不泄漏明文账号名。
- **环境强绑定警告**：当前 Chrome Profile 位于 Windows 环境下，Cookies 数据库依赖 Windows DPAPI 加密，**严禁直接将该目录复制到 Linux/NAS 容器中运行**。
- **服务端失效陷阱**：客户端 Cookie 未过期并不等于服务端会话有效，当收到抖音 WAF 返回的 403/Challenge 时，必须触发交互式验证恢复。

---

# 18. F2 Integration (F2 抓取引擎深度集成说明)

- **锁定版本**：`f2 == 0.0.1.7`（运行在 `.venv-f2` 隔离环境）
- **集成模式**：进程内 Python API 直接调用（`F2InProcessBackendAdapter`），非子进程命令行拉起。
- **安全加固措施**：
  1. **动态签名支持**：集成 F2 内部签名逻辑生成请求级动态 `msToken` 与 `a_bogus`。
  2. **输出绝对重定向**：通过强行覆盖 F2 的输出配置，将媒体文件强制约束在 Task Sandbox 的 `output/` 目录下，防止散落到项目根目录。
  3. **进程级执行排他锁**：确保同一进程内同一时刻仅能执行一个 F2 下载实例，防止全局静态配置冲突。
  4. **Monkeypatch 现场还原**：下载前后自动备份并恢复 F2 的全局配置字典、日志记录器及进程当前工作目录（CWD）。
- **升级敏感性警告**：F2 内部类（如 `DouyinDownloader`, `AsyncUser`）为非公开私有 API，未来升级任何 F2 小版本必须重新跑通 D03/D10 全量回归。

---

# 19. Incremental Sync Algorithm (C05 增量同步与水位推进机制)

增量同步引擎（`DouyinSyncEngine`）严格遵循 QW-04 / QW-15 冻结规范与当前生产实现（`src/collector/douyin/sync_engine.py`, `sync_policy.py`, `repository.py`），核心逻辑如下：

### 19.1 核心概念澄清
1. **`create_time`（发布时间）**：抖音服务端返回的作品发布时间戳。**创作者发布作品的时间绝不等于用户点击收藏的时间**，因此系统**严禁**使用 `create_time > cursor` 判断是否是新收藏！
2. **`cursor`（分页与同步边界标记）**：单调递减的不透明游标字符串（TEXT 格式存储），表示在收藏列表中的分页位置。
3. **新旧收藏判定依据（Prior Collection State）**：系统在遍历页面作品时，通过 `repository.get_prior_state(platform_content_id, scope_id, platform)` 查询持久化仓储：
   - 数据库中无记录：判定为【新发现收藏】（`is_first_observation = 1`），计入 `new_items`。
   - 数据库已有记录且为 active：判定为【已知作品】（`known_items`）。
   - 数据库已有记录但此前为 inactive：判定为【重新收藏】（`is_reappearance = 1`），计入 `reappeared_items`。
4. **策略 A 禁令**：**严格禁止遇到第一个已知作品就立即停机（Strategy A is strictly prohibited）**！因为用户可能在历史收藏作品之间取消再重新收藏，过早停机会导致漏扫。

### 19.2 增量同步执行流水线
```
[ 触发采集: collector douyin sync ]
                  │
                  ├── 1. 读取 DB 状态: 
                  │      committed_watermark = incremental_head_watermark_cursor (TEXT)
                  │      history_complete = sync_state.history_complete (0 或 1)
                  │
                  ├── 2. 初始化分页游标:
                  │      current_request_cursor = "0" (日常增量永远从 "0" 头部开始)
                  │
                  ▼
         [ 逐页拉取与处理循环 ]
                  │
                  ├── 2.1 C04 SourceClient 拉取第 page_number 页数据
                  │
                  ├── 2.2 C06 RawArchiver 写入原始 JSON 证据 (raw/ 目录)
                  │
                  ├── 2.3 遍历页内条目:
                  │     ├── 查询 DB 前序状态: repo.get_prior_state(platform_content_id, ...)
                  │     ├── C07 Transformer 转换为 CanonicalItem
                  │     └── C08 Repository 暂存至 run_item_staging 临时表
                  │
                  ├── 2.4 Page 1 特殊处理:
                  │     └── 记录 candidate_watermark = page.response_cursor (捕获最新头部游标)
                  │
                  ▼
         [ QW-04 IncrementalSyncPolicy 评估本页事实 (PageFacts) ]
                  │
                  ├── A. 终端结束 (STOP_TERMINAL):
                  │      条件: has_more == 0 或 response_cursor == "0" 或 items_count == 0
                  │      动作: 停止分页，标记 history_complete = True
                  │
                  ├── B. 安全边界停机 (STOP_SAFE_BOUNDARY):
                  │      条件: mode == "incremental" 并且 history_complete == True 
                  │            并且 int(response_cursor) <= int(committed_watermark)
                  │      动作: 触发 WATERMARK_REACHED 停机，复用已完备历史
                  │
                  ├── C. 分页超限停机 (STOP_LIMIT):
                  │      条件: page_number >= max_pages 或 达到 backfill_limit
                  │      动作: candidate_watermark 置空 (严禁超限停机推进头部水位!)
                  │
                  └── D. 继续下一页 (CONTINUE):
                         更新 current_request_cursor = page.response_cursor，继续拉取
                  │
                  ▼
         [ 事务终态提交: finalize_success / finalize_failure ]
                  │
                  ├── 仅当成功完成且 candidate_watermark 非空时:
                  │     原子更新 sync_state:
                  │       incremental_head_watermark_cursor = candidate_watermark
                  │       history_complete = new_history_complete
                  │     将 staging 暂存条目批量合入 collection_items / observations
                  │     写入 download_outbox (status='PENDING')
                  │
                  └── 若运行失败或 Dry-Run:
                        清空 run_item_staging，原水位与主表保持绝对不动
```

### 19.3 字段演进与隔离
- `incremental_head_watermark_cursor`：当前生产活跃使用的头部增量水位游标。
- `committed_watermark_cursor`：v1 迁移旧字段，已由 v2 迁移平滑升级。
- `backfill_checkpoint_cursor`：全量历史回溯未完备时记录的断点游标，供 `--resume` 恢复使用，与增量头部水位完全解耦。

---

# 20. Downloader Worker State Machine (Worker 作业与 Outbox 状态机)

```
[ Collector 事务 ]
   │
   ▼
[ download_outbox ]
   │
   ├── PENDING (新产生意图，等待认领)
   │     │
   │     │ (OutboxConsumerBridge 批量领单，写入 DownloaderJobStore)
   │     ▼
   ├── DISPATCHED (已持久化交付给 Downloader 作业池)
   │
   └── FAILED (仅当无法持久化到 Downloader DB 时报错)

═════════════════════════════════════════════════════════════════════════

[ DownloaderJobStore (download_jobs) ]
   │
   ├── READY (作业就绪，排队等待 Worker 线程认领)
   │     │
   │     │ (Worker 成功抢占 ServiceLock 并 Claim 作业)
   │     ▼
   ├── RUNNING (Worker 正在执行 8 阶段下载状态机)
   │     │
   │     ├── [执行成功] ──> SUCCEEDED (正式资产归档提交成功，终态)
   │     │
   │     ├── [临时性故障] ──> RETRY_WAIT (按退避策略计算冷却时间，到期转 READY)
   │     │
   │     ├── [风控/人机] ──> BLOCKED_AUTH (挂起作用域，等待用户交互完成验证)
   │     │
   │     └── [不可逆错误] ──> TERMINAL_FAILED (达到最大重试或不可修复，终态)
```

---

# 21. Downloader Error Taxonomy & Retry Policy (D08 错误分类与退避重试矩阵)

下载器错误处理与重试策略由 `src/downloader/retry_policy.py` 中的 `ProductionDownloadErrorPolicy` 引擎驱动，严格依据 `src/downloader/contracts.py` 中定义的 `DownloaderErrorCode` 与 `RetryAction` 进行确定性裁决：

### 21.1 核心策略矩阵 (100% 对齐当前代码 Enum)

| 规范错误码 (`DownloaderErrorCode`) | 归属类别 | 策略决策动作 (`RetryAction`) | 允许尝试预算 (`max_attempts`) | 重试间隔与行为特征 | 详细说明 |
| :--- | :--- | :---: | :---: | :--- | :--- |
| `DOWNLOAD_VALIDATION_FAILED` | Tool & Pipeline | `RETRY_AFTER_RERESOLVE` $\rightarrow$ `TERMINAL` | **2** (初次 + 1 次重试) | 首次失败触发 `requires_reresolve=True` + 指数退避；二次失败终结 | **QW-13 冻结契约**：首次校验失败（媒体损坏/不完整）允许重新解析并拉取流 1 次以排除传输瞬时抖动；重试再次失败则判定为永久损坏，终止重试 |
| `DOWNLOAD_AUTH_REQUIRED` | Auth & Security | `BLOCKED_AUTH` | 1 (挂起) | `retryable=False`, 暂停该 scope 调度, `worker_action=PAUSE_SCOPE` | 账号未登录或会话凭据已过期，阻断盲目连续重试，等待人工介入刷新会话 |
| `DOWNLOAD_AUTH_CHALLENGE` | Auth & Security | `BLOCKED_AUTH` | 1 (挂起) | `retryable=False`, 暂停该 scope 调度, `worker_action=PAUSE_SCOPE` | 触发滑块人机验证或安全挑战，必须由人工在浏览器中完成验证恢复 |
| `DOWNLOAD_CREDENTIAL_BRIDGE_FAILED` | Auth & Security | `RETRY_AFTER_CREDENTIAL_REFRESH` 或 `TERMINAL` | 结构性 1 次 / 临时性 2 次 | 临时故障退避 1~10s 并要求刷新快照；结构性配置错误直接终结 | 结构性作用域不匹配不可重试；进程间连接断开或超时允许刷新重试 |
| `DOWNLOAD_RATE_LIMITED` | Platform & Remote | `WAIT_AND_RETRY` | 3 | 解析 `Retry-After` 头，钳位在 [1.0s, 300.0s]，默认 60s | 命中平台 429 频率限制，必须严格遵从服务端冷却指示，暂停该作用域 |
| `DOWNLOAD_NETWORK_ERROR` | Network & Infra | `RETRY` | 3 | 指数退避: $2.0 \times 2.0^{(n-1)}$，上限 60s，带 20% 抖动 | 网络连接断开、DNS 抖动、CDN 握手失败等瞬时网络异常 |
| `DOWNLOAD_TIMEOUT` | Network & Infra | `RETRY` | 3 | 指数退避: $2.0 \times 2.0^{(n-1)}$，上限 60s，带 20% 抖动 | HTTP 请求或传输超时 |
| `DOWNLOAD_SERVER_ERROR` | Platform & Remote | `RETRY` | 3 | 指数退避: $2.0 \times 2.0^{(n-1)}$，上限 60s，带 20% 抖动 | 抖音服务器端 5xx 内部错误 |
| `DOWNLOAD_NOT_FOUND` / `DOWNLOAD_UNAVAILABLE_DELETED` | Platform & Remote | `RETRY_AFTER_RERESOLVE` 或 `TERMINAL` | CDN 2 次 / 永久 1 次 | 签名过期/CDN 404 允许重解析 1 次；作品被作者彻底删除则立即终态终止 | 区分易失性 CDN 链接失效与平台永久删除 |
| `DOWNLOAD_MEDIA_INCOMPLETE` | Tool & Pipeline | `RETRY_AFTER_RERESOLVE` | 2 | 退避重试并要求重新解析源媒体流 | 抓取到的媒体字节不全或被截断 |
| `DOWNLOAD_TOOL_ERROR` | Tool & Pipeline | `TERMINAL` | 1 | `retryable=False`, `worker_action=WORKER_UNHEALTHY` | 宿主机环境缺少 ffmpeg/ffprobe 或执行严重出错，标识 Worker 不健康 |
| `SANDBOX_CREATE_FAILED` / `SANDBOX_PATH_ESCAPE` / `SANDBOX_METADATA_CORRUPT` / `SANDBOX_CLEANUP_FAILED` | Tool & Pipeline | `TERMINAL` | 1 | `retryable=False`, `worker_action=WORKER_UNHEALTHY` | 沙箱文件系统故障或路径外溢违规，立即阻断并告警 |
| `DOWNLOAD_INVALID_INPUT` / `DOWNLOAD_UNSUPPORTED_CONTENT` | Input Validation | `TERMINAL` | 1 | 立即终止，不重试 | 输入 schema 不合法或内容类型不支持 |
| `DOWNLOAD_PERMISSION_DENIED` | Platform & Remote | `TERMINAL` | 1 | 立即终止，不重试 | 作品私密、无权限查看 (HTTP 403) |
| `DOWNLOAD_UNKNOWN` | Tool & Pipeline | `RETRY` | 2 | 基础退避重试 | 未识别的偶发异常 |

> [!NOTE]
> **归档冲突处理规则 (Archive Conflict Subreason Rule)**：若在沙箱向正式归档提升阶段检测到目标目录已存在且 SHA-256 冲突（subreason 包含 `ARCHIVE_CONFLICT` 或 `PROMOTION_CONFLICT`），`ProductionDownloadErrorPolicy` 判定动作一律为 `TERMINAL`（拒绝覆盖物理目标，需人工排查）。

---

# 22. Formal Asset Validity (正式物理资产判定准则)

判断一个归档目录是否为合法的 Formal Local Asset，**绝不能只靠检查 `.mp4` 或 `.webp` 文件是否存在**。必须调用标准探针函数：
```python
from src.downloader.promoter import verify_archived_asset

result = verify_archived_asset("G:/local_pc_project/archive/douyin/7681603850364521734")
assert result.valid is True
```
### 判定合法的四重充分必要条件：
1. **目录合法性**：目标路径存在且为物理目录。
2. **提交承诺清单**：`asset_manifest.json` 物理存在且为合法 JSON。
3. **资产物理完整性**：清单中 `assets` 列表声明的所有文件必须在目录内物理存在，且字节大小完全一致。
4. **强密码学哈希吻合**：计算每个物理文件的 SHA-256 摘要，必须与清单中记录的 `sha256` 字符串 100% 吻合。

---

# 23. Security Model (安全架构与凭证防泄漏模型)

1. **契约级脱敏**：`DownloadTask` 契约禁止传递任何 Cookie 或 Token；命令行参数（argv）与环境变量（env）禁止传递 Cookie。
2. **存储防泄漏**：`metadata.db` 与 `downloader_state.sqlite3` 中的所有 JSON 字段在落盘前均通过 `scrub_secrets()` 过滤。
3. **日志脱敏防爆**：应用层日志记录器针对 `sessionid`, `sid_guard`, `passport_csrf_token`, `authorization` 施行实时正则脱敏替换为 `[REDACTED]`。
4. **沙箱逃逸防御 (Path Containment)**：D04 沙箱严控路径包含，拒绝跨目录 `../` 与绝对路径外溢。
5. **安全局限性坦承**：D04 是 **应用级路径约束 (Application-level Containment)**，不是 Linux cgroups 或 Windows AppContainer 级别的操作系统内核沙箱。F2 Worker 依然拥有当前执行用户的物理权限。

---

# 24. Process Ownership & Service Lock Storage (进程所有权与锁文件存储机制)

为了防止多进程同时拉起导致并发竞态冲突，`DownloaderServiceLock` 实现了工业级的三元组身份校验与文件排他锁机制：

### 24.1 身份三元组定义 (WorkerIdentity)
```
WorkerIdentity:
  - pid: 操作系统进程 PID
  - process_started_at: 进程创建时间戳 (精度微秒)
  - instance_id: 服务实例 UUID
```
### 24.2 权威锁文件存储机制 (`data/.worker.lock`)
- **文件存储位置**：`G:\local_pc_project\data\.worker.lock`（专用物理 JSON 文件）。
- **文件内容结构**：
  ```json
  {
    "pid": 12345,
    "process_started_at": 1788701234.5678,
    "instance_id": "inst_d9f8e4a2",
    "worker_id": "12345:1788701234.567800:inst_d9f8e4a2",
    "acquired_at": 1788701234.600,
    "heartbeat_at": 1788701240.000
  }
  ```
- **与 SQLite 的边界划分**：`downloader_state.sqlite3` 中**不建 `service_locks` 表**。锁排他由该物理文件强力保证；而在 SQLite 内部，单个作业的归属仅通过 `download_jobs.claimed_by` 字段记录当前 WorkerIdentity 序列化字符串。

### 24.3 锁抢占与孤儿回收法则
1. **PID 不存在**：直接判定前任已死，安全清理锁文件并夺取。
2. **PID 存在但创建时间不同**：证明原 PID 已被操作系统回收分配给无关新进程，判定前任已死，安全夺取。
3. **PID 存在且创建时间相同**：证明原属主 Worker 仍存活于系统中。**哪怕锁文件的修改时间或心跳已大幅超时，也严禁抢占锁！**
4. **崩溃恢复 (`startup_recovery`)**：仅当新 Worker 确定自身合法持有 `ServiceLock` 时，才执行作业池扫描，将因非正常崩溃遗留的 `RUNNING` 孤儿作业重置回 `READY`。

---

# 25. Exact Startup & Shutdown Runbook (精准启动与优雅停止手册)

本章节提供完全基于当前实际工程 CLI 的 PowerShell 操作命令。

### 25.1 步骤 1：检查专用 Chrome Profile 状态 [OFFLINE]
```powershell
# 检查专用 Profile 及 Cookies 数据库是否存在
Test-Path "G:\antigravity-cli\dy\runtime\chrome-profile\Default\Network\Cookies"
# 预期输出: True
```

### 25.2 步骤 2：执行采集器认证探测 (Collector Probe) [READ-ONLY NETWORK]
```powershell
cd G:\local_pc_project
& "G:\local_pc_project\.venv\Scripts\python.exe" -m src.collector.cli douyin probe
# 预期输出: 探测成功，显示 source_health: HEALTHY, auth_state: AUTH_VALID
```

### 25.3 步骤 3：单次运行增量采集同步 (Collector Incremental Sync) [NETWORK]
```powershell
cd G:\local_pc_project
& "G:\local_pc_project\.venv\Scripts\python.exe" -m src.collector.cli douyin sync --mode incremental
# 预期行为:
# 1. 自动探查最新收藏，对比 DB 前序状态识别新项
# 2. 触碰增量水位线后自动安全停机 (StopReason.WATERMARK_REACHED)
# 3. 新作品原子存入 collection_items 并向 download_outbox 排队任务
```

### 25.4 步骤 4：前台启动 Downloader Worker 消费处理 [NETWORK + COMPUTE]
```powershell
cd G:\local_pc_project
# 必须使用 .venv-f2 环境执行 Worker
& "G:\local_pc_project\.venv-f2\Scripts\python.exe" -m src.downloader.worker run --max-batches 1
# 预期行为:
# 1. 获取 ServiceLock 文件锁 (data/.worker.lock)
# 2. OutboxConsumerBridge 批量认领 PENDING 任务转为 DISPATCHED
# 3. 调度 F2 完成真实下载、沙箱隔离、规范命名、容器探测、解码冒烟与原子归档
```

### 25.5 步骤 5：优雅停机与服务退出 (Graceful Shutdown)
- **前台运行控制**：在运行 Worker 的终端直接按下 `Ctrl + C`。
- **信号捕获机制**：Worker 拦截 `SIGINT` 与 `SIGTERM`，完成当前批次已处于 `PROMOTING` 的关键写操作后，安全释放 `data/.worker.lock` 并退出。
- **停机核验命令**：
```powershell
# 核验锁文件是否已安全释放 (返回 False 为正常)
Test-Path "G:\local_pc_project\data\.worker.lock"
```

---

# 26. Complete Acceptance Verification Matrix (完整验收核验矩阵)

| 验证环节 | 实际验证命令 | 达成标志 / 预期输出 |
| :--- | :--- | :--- |
| **1. 离线全量主回归** | `& G:\local_pc_project\.venv\Scripts\pytest.exe -q` | **672 passed, 4 skipped** |
| **2. F2 Worker 隔离回归** | `& G:\local_pc_project\.venv-f2\Scripts\pytest.exe tests/test_downloader_worker.py -k "not live" -q` | **55 passed, 10 deselected** |
| **3. C10 综合 E2E 验证** | `& G:\local_pc_project\.venv\Scripts\pytest.exe tests/test_collector_downloader_e2e.py -q` | **5 passed** |
| **4. 视频资产完整性** | `python -c "import sys; sys.path.insert(0, 'G:/local_pc_project'); from src.downloader.promoter import verify_archived_asset; r = verify_archived_asset('G:/local_pc_project/archive/douyin/7681603850364521734'); print(r.valid)"` | 输出: `True` |
| **5. 图集资产完整性** | `python -c "import sys; sys.path.insert(0, 'G:/local_pc_project'); from src.downloader.promoter import verify_archived_asset; r = verify_archived_asset('G:/local_pc_project/archive/douyin/7682038498466993905'); print(r.valid)"` | 输出: `True` |
| **6. 认证探针检查** | `& G:\local_pc_project\.venv\Scripts\python.exe -m src.collector.cli douyin probe` | `source_health=HEALTHY`, `auth_state=AUTH_VALID` |
| **7. 数据库只读完整性** | `python -c "import sqlite3; [print(f'{db}:', sqlite3.connect(f'G:/local_pc_project/data/{db}').execute('PRAGMA integrity_check').fetchone()[0]) for db in ['metadata.db', 'downloader_state.sqlite3']]"` | 两个库均返回 `ok` |

---

# 27. Test Suites & Verification Inventory (测试工程资产索引)

### 27.1 测试套件执行基准统计
- **Main Test Suite (`.venv`)**：
  - 执行命令：`pytest`
  - 结果：**`672 passed, 4 skipped`** (耗时 ~27.8s)
- **Worker Test Suite (`.venv-f2`)**：
  - 执行命令：`pytest tests/test_downloader_worker.py -k "not live"`
  - 结果：**`55 passed, 10 deselected`**

### 27.2 核心测试文件分布索引 (全部经过物理路径核验)
- [`tests/test_collector_downloader_e2e.py`](file:///G:/local_pc_project/tests/test_collector_downloader_e2e.py): 端到端集成流水线 5 大场景全覆盖。
- [`tests/test_downloader_worker.py`](file:///G:/local_pc_project/tests/test_downloader_worker.py): Worker 服务锁、孤儿恢复与调度测试。
- [`tests/test_f2_backend.py`](file:///G:/local_pc_project/tests/test_f2_backend.py): F2 进程内抓取适配器与错误模拟测试。
- [`tests/test_content_router.py`](file:///G:/local_pc_project/tests/test_content_router.py): 视频与图集路由计划与异常拒绝测试。
- [`tests/test_retry_policy.py`](file:///G:/local_pc_project/tests/test_retry_policy.py): 规范错误码重试策略与退避算法测试。
- [`tests/test_archive_promoter.py`](file:///G:/local_pc_project/tests/test_archive_promoter.py): 最终物理资产原子提升与 manifest 校验测试。
- [`tests/test_asset_normalizer.py`](file:///G:/local_pc_project/tests/test_asset_normalizer.py): 1..N 连续图像序号与长路径预算测试。
- [`tests/test_media_validator.py`](file:///G:/local_pc_project/tests/test_media_validator.py): ffprobe 结构探针与格式校验测试。
- [`tests/test_task_sandbox.py`](file:///G:/local_pc_project/tests/test_task_sandbox.py): UUID 沙箱隔离、外溢拦截与 GC 测试。
- [`tests/test_credentials.py`](file:///G:/local_pc_project/tests/test_credentials.py): 凭证快照隔离、上下文管理与防御性测试。

---

# 28. Known Limitations & Deferred Work (已知局限与延期事项)

以下项目为经过明确确认的已知边界或后续技术债，**不属于 M2 的阻塞项**：

1. **平台反爬与接口脆弱性**：抖音私有 API 字段可能发生变更，需持续关注；若遭遇强人机验证，需通过有头浏览器人工拖动滑块恢复。
2. **专用 Profile 的系统局限**：当前 Chromium Profile 位于 Windows 宿主机，依赖本地环境 DPAPI 解密，无法直接挂载给 Linux/NAS Docker 运行。
3. **快照轮询的捕获盲区**：若用户在两次轮询间隙完成“收藏立即又取消收藏”，系统无法观察到该过程。
4. **历史真实收藏时序不可知**：抖音 API 不提供真实收藏操作的时间戳，系统仅能依据观察时序进行全序排列。
5. **单任务执行设计**：当前 Worker 架构遵循保守风控原则，采用单进程串行调度 F2，未开启高并发多任务并发下载。
6. **网络存储原子性未验证**：正式归档基于本地 NTFS 的原子移动机制，若未来迁移至 SMB/NFS 等 NAS 网络共享盘，原子性需重新评估。
7. **多模态与知识库未启动**：ASR/OCR/VLM 处理、RAG 向量库与 Obsidian 双链集成全部留待 M3+。

---

# 29. Failure & Troubleshooting Guide (故障排查速查指南)

| 故障现象 (Symptom) | 涉及核心组件 | 第一检查排查项 (First Check) | 绝对禁止操作 (DO NOT DO) |
| :--- | :---: | :--- | :--- |
| `DOWNLOAD_AUTH_REQUIRED` / 403 | C03 / D02 | 运行 `python -m src.collector.cli douyin probe`，检查 Chrome 是否弹出验证码 | **严禁**硬编码伪造 Cookie 或盲目重试冲击 WAF |
| 429 Too Many Requests | D08 | 检查 `downloader_state.sqlite3` 中 `scope_pauses` 表的解禁时间戳 | **严禁**清空退避计时器连续发起高频重试 |
| F2 报找不到包或环境报错 | D03 | 检查 Worker 是否在 `.venv-f2` 下运行（检查 `python -c "import f2"`） | **严禁**在主环境 `.venv` 中盲目 `pip install f2` |
| `DOWNLOAD_TOOL_ERROR` (缺少工具) | D05 | 运行 `where ffmpeg` 与 `where ffprobe`，检查二进制是否在 PATH 中 | **严禁**跳过验证步骤直接归档损坏文件 |
| `Worker lock held` 无法启动 | D10 / Lock | 检查 `data/.worker.lock` 中记录的 PID 进程是否在任务管理器中真实存活 | **严禁**不查 PID 直接无脑强删锁文件 |
| 孤儿作业卡在 `RUNNING` 状态 | D10 / Store | 重启 Worker，观察 `startup_recovery()` 日志是否将其安全重置为 `READY` | **严禁**手动直接 SQL 修改任务为 TERMINAL_FAILED |
| Outbox 卡在 `PENDING` 不流转 | C09 / D10 | 检查 Downloader Worker 是否已启动，检查 `data/.worker.lock` 是否被占 | **严禁**手动将 Outbox 强行置为 DISPATCHED |
| 归档资产 `verify` 报 False | D07 | 比对 `asset_manifest.json` 与目录内文件 SHA-256，排查文件被占用或损坏 | **严禁**手动篡改 manifest 的哈希值绕过报错 |
| 图集报文件序列不匹配 | D06 | 检查沙箱 `output/` 原始图片命名，排查是否有下载缺失 | **严禁**伪造空文件凑齐序号 |

---

# 30. Recovery & Backup Critical Files (灾备与关键数据资产恢复指南)

> [!CAUTION]
> ### CRITICAL RECOVERY NOTICE: 本地工作区完整灾备不可替代
> 如 Section 1 所述，Git 远程仓库未包含本地修改及 54 个未跟踪模块。**灾备恢复必须完整备份以下物理路径**：

| 资产等级 | 资产类别 | 实际绝对路径 | 备份策略 / 恢复方法 |
| :---: | :--- | :--- | :--- |
| **CRITICAL** | **代码与测试仓库** | `G:\local_pc_project` | **物理全量备份整个工作目录**（单纯 `git clone` 无法恢复 M2） |
| **CRITICAL** | **元数据库** | `G:\local_pc_project\data\metadata.db` | 定期 SQLite `.backup` 冷备，严禁直接删库 |
| **CRITICAL** | **下载状态库** | `G:\local_pc_project\data\downloader_state.sqlite3`| 记录作业执行历史，定期冷备 |
| **CRITICAL** | **正式归档资产** | `G:\local_pc_project\archive` | 包含所有下载完成的高清视频与图集，需重点持久化 |
| **CRITICAL** | **原始响应证据** | `G:\local_pc_project\data\raw` | 采集真实性审计证据链，建议保留 |
| **CRITICAL** | **认证配置文件** | `G:\antigravity-cli\dy\runtime\chrome-profile` | 宿主机登录会话，如换机需重新扫码登录生成 |
| **REGENERABLE**| 任务隔离沙箱 | `G:\local_pc_project\data\sandbox` | 临时运行目录，崩溃或成功后均可由 GC 安全清理 |
| **REGENERABLE**| 运行日志文件 | `G:\local_pc_project\logs` | 历史诊断日志，可定期归档或清理 |
| **REGENERABLE**| 虚拟运行环境 | `G:\local_pc_project\.venv` & `.venv-f2` | 可由 `requirements.txt` 重新安装重建 |

---

# 31. Git & Repository State (版本控制实况)

- **当前分支**：`main`
- **当前 HEAD Commit**：`29ee1e97246ceeffd9454de28a331fb96bc7c6c1`
- **代码库实况分析**：
  - 11 个追踪文件受修改（包含配置文件、文档及历史代码适配）。
  - 54 个未跟踪模块与测试（`src/collector/`, `src/downloader/`, `tests/test_*`）。
  - **规范约束**：保持当前工作树现状，不要为“收尾”强行执行未经规划的 Git Commit 或 Reset。
- **.gitignore 忽略项**：
  - `data/` 目录中的 SQLite 数据库（`*.db`, `*.sqlite3`）
  - `archive/` 物理归档媒体目录
  - `runtime/chrome-profile` 用户隐私目录
  - `.venv*/` 虚拟环境目录

---

# 32. Vikunja Snapshot (项目看板实况对照)

截至 2026-09-07，Vikunja 项目看板（`http://192.168.1.191:3456`, Project 29: `E3 · Douyin Collection Ingestion`）状态核实如下：

- **Milestone M2 专属任务全量 100% DONE**：
  - `Task #35 ~ #44` (DY-C01 ~ DY-C10) 状态全部为 `done: true`。
  - `Task #55 ~ #64` (DY-D01 ~ DY-D10) 状态全部为 `done: true`。
  - Task #44 (DY-C10) 与 Task #64 (DY-D10) 均已归档至 Bucket 213 (Done)。
- **说明**：Task #26 和 #27 为早期 Docker Chromium 预研探索任务，不属于 M2 的交付范围。

---

# 33. Historical Reports Index (历史技术报告权威索引)

所有历史报告均持久化保存在 [`G:\antigravity-cli\dy\`](file:///G:/antigravity-cli/dy) 目录下：

### 33.1 预研与方案论证报告 (QW 系列)
- [`QW-03_final.md`](file:///G:/antigravity-cli/dy/QW-03_final.md): 抖音收藏夹与点赞接口协议反向工程成果（说明 likes 与 collection 的本质区别）。
- [`QW-04_incremental_stop.md`](file:///G:/antigravity-cli/dy/QW-04_incremental_stop.md): 增量停机水位线理论验证报告。
- [`QW-05_browser_auth_persistence.md`](file:///G:/antigravity-cli/dy/QW-05_browser_auth_persistence.md): 专属 Chrome Profile 持久化登录态保持验证。
- [`QW-06_auth_failure_detection.md`](file:///G:/antigravity-cli/dy/QW-06_auth_failure_detection.md): 认证失效、403 与人机验证捕获状态机。
- [`QW-09_f2_single_download.md`](file:///G:/antigravity-cli/dy/QW-09_f2_single_download.md): F2 单作品下载适配初探。
- [`QW-10_f2_multi_scenario.md`](file:///G:/antigravity-cli/dy/QW-10_f2_multi_scenario.md): F2 多场景（视频/图集/异常）综合测试。
- [`QW-11_download_auth_bridge.md`](file:///G:/antigravity-cli/dy/QW-11_download_auth_bridge.md): 采集层向下载层透传凭证的内存安全桥接方案。
- [`QW-12_collection_schema.md`](file:///G:/antigravity-cli/dy/QW-12_collection_schema.md): 统一 Canonical 实体与元数据模式设计。
- [`QW-14_douyin_handoff.md`](file:///G:/antigravity-cli/dy/QW-14_douyin_handoff.md): M1 阶段收尾交接总结（已被本文档全量承接并更新）。

### 33.2 采集器实施报告 (DY-C 系列)
- [`DY-C01_collector_skeleton.md`](file:///G:/antigravity-cli/dy/DY-C01_collector_skeleton.md): 采集器服务骨架设计与 CLI 落地。
- [`DY-C02_browser_runtime.md`](file:///G:/antigravity-cli/dy/DY-C02_browser_runtime.md): 浏览器运行时挂载与连接治理。
- [`DY-C03_auth_state_integration.md`](file:///G:/antigravity-cli/dy/DY-C03_auth_state_integration.md): 鉴权状态机与防御机制实现。
- [`DY-C04_listcollection_client.md`](file:///G:/antigravity-cli/dy/DY-C04_listcollection_client.md): 浏览器上下文无感列表接口代理客户端。
- [`DY-C05_incremental_sync_engine.md`](file:///G:/antigravity-cli/dy/DY-C05_incremental_sync_engine.md): 增量头部水位与历史回溯引擎。
- [`DY-C06_raw_response_archiver.md`](file:///G:/antigravity-cli/dy/DY-C06_raw_response_archiver.md): 原始响应证据沉淀器实现。
- [`DY-C07_canonical_transform.md`](file:///G:/antigravity-cli/dy/DY-C07_canonical_transform.md): 结构化清洗引擎与契约落地。
- [`DY-C08_metadata_persistence.md`](file:///G:/antigravity-cli/dy/DY-C08_metadata_persistence.md): SQLite 元数据库事务性仓储落盘。
- [`DY-C09_download_queue_producer.md`](file:///G:/antigravity-cli/dy/DY-C09_download_queue_producer.md): 事务性 Outbox 异步队列解耦引擎。
- [`DY-C10_collector_downloader_e2e.md`](file:///G:/antigravity-cli/dy/DY-C10_collector_downloader_e2e.md): 采集器与下载器端到端真实业务验收与收尾审计总报告。

### 33.3 下载器实施报告 (DY-D 系列)
- [`DY-D01_safe_downloader_refactor.md`](file:///G:/antigravity-cli/dy/DY-D01_safe_downloader_refactor.md): 8 阶段下载状态机执行器重构。
- [`DY-D02_credential_provider.md`](file:///G:/antigravity-cli/dy/DY-D02_credential_provider.md): 进程内凭证快照隔离提供者。
- [`DY-D03_f2_backend.md`](file:///G:/antigravity-cli/dy/DY-D03_f2_backend.md): F2 进程内适配器与沙箱环境重定向。
- [`DY-D04_task_sandbox.md`](file:///G:/antigravity-cli/dy/DY-D04_task_sandbox.md): UUID 任务尝试沙箱与孤儿垃圾回收。
- [`DY-D05_media_validator.md`](file:///G:/antigravity-cli/dy/DY-D05_media_validator.md): 多阶段媒体有效性与容器探针校验器。
- [`DY-D06_asset_normalizer.md`](file:///G:/antigravity-cli/dy/DY-D06_asset_normalizer.md): 资产确定性规范化命名与长路径防护。
- [`DY-D07_atomic_archive_promotion.md`](file:///G:/antigravity-cli/dy/DY-D07_atomic_archive_promotion.md): 正式物理资产原子提升与 manifest 清单落盘。
- [`DY-D08_retry_policy.md`](file:///G:/antigravity-cli/dy/DY-D08_retry_policy.md): 下载异常分类与智能退避策略。
- [`DY-D09_content_router.md`](file:///G:/antigravity-cli/dy/DY-D09_content_router.md): 基于内容类型的确定性执行路由器。
- [`DY-D10_downloader_worker_e2e.md`](file:///G:/antigravity-cli/dy/DY-D10_downloader_worker_e2e.md): Downloader Worker 守护调度与最终端到端测试。

---

# 34. Source-of-Truth Priority & Superseded Historical Facts (事实真相裁决与历史修正清册)

### 34.1 事实真相仲裁优先级
未来维护中若发现不同文档或代码之间存在表述冲突，按以下严苛优先级进行裁决：
1. **当前生产代码契约 (`src/`)**：最新已落盘并通过测试的实际代码与类型定义为最高准则。
2. **本主交接文档 (`M2_DOUYIN_COMPLETE_HANDOFF.md`)**：作为当前阶段体系集大成的唯一权威说明。
3. **最终收尾验收报告 (`DY-C10_collector_downloader_e2e.md` & `DY-D10_downloader_worker_e2e.md`)**。
4. **单项任务实施报告 (`DY-C01~C09`, `DY-D01~D09`)**。
5. **早期总交接文档 (`QW-14_douyin_handoff.md`)**。
6. **早期单项研究报告 (`QW-03 ~ QW-13`)**。
7. **历史对话摘要或口头假设**（最低优先级，不可作为技术决策依据）。

### 34.2 历史认知演进与已知差异修正清册 (Superseded Historical Facts)
为避免后续 Agent 查阅早期文档时产生认知困惑，特此固化以下已知演进与修正：
1. **C10 视频资产元数据历史笔误修正 (Video Metadata Historical Discrepancy)**：
   - 早期交接草稿中曾误将视频资产 `7681603850364521734` 描述为 `2,725,532 bytes, 1080x1920, h264/aac, 6.64 sec`。
   - **真实物理 Ground Truth**：该物理文件自 C10 下载落盘以来，哈希始终为 `3959a0561c58cea93b2d9093f66bf5b306afa888b914657140aa6c2b01bee7ad`，实际大小为 **173,847,684 字节** (~165.8 MB)，为 **HEVC 4K (2160x3840)**、AAC 音频、时长 **371.71 秒** (~6.19 分钟)。旧草稿数值属于文档编撰时的样本数值混淆，现已依据物理文件与 manifest 彻底纠正。
2. **增量水位快照历史差异说明 (Watermark Snapshot Historical Discrepancy)**：
   - 当前生产 SQLite 数据库 `metadata.db` 中 `sync_state` 实读 Ground Truth 为：  
     `incremental_head_watermark_cursor = "1788536236926781"`，`committed_watermark_cursor = "1788536236926781"`，`last_successful_sync_run_id = "sync_douyin_coll_20260906_152841_38ee94"`。
   - 早期草稿曾出现 `1788708404000`（系当时对话中根据毫秒时间戳非正式口头推导的值），属于 `HISTORICAL REPORTING DISCREPANCY`。生产代码直接操作字符串游标 `"1788536236926781"`，以数据库持久化记录为唯一准绳。
3. **增量停机判定准则修正**：早期文档曾口头假设“比较发布时间与水位游标”，生产实现已严格明确——`create_time` 是视频创作者的发布时间，系统新旧收藏判定完全依赖持久化仓储中的先验记录（`prior_state`），停机条件严格由分页游标边界判定（`int(response_cursor) <= int(committed_watermark)` 或 `has_more == 0`）。
4. **水位游标字段演进**：Schema v1 使用单一 `committed_watermark_cursor`；Schema v2 正式拆分为 `incremental_head_watermark_cursor`（头部增量水位）与 `backfill_checkpoint_cursor`（回溯断点游标），并引入 `history_complete` 标志。
5. **下载错误分类表述演进**：早期开发过程记录中曾非正式混用基于数字计数的异常分类话术，权威定义已由 `src/downloader/contracts.py` 中的 `DownloaderErrorCode` 枚举与 `ProductionDownloadErrorPolicy` 确定性决策引擎统一收敛。
6. **51 条 Outbox 与 2 个 Jobs 对账**：`metadata.db` 中的 51 条记录包含了早期 C08/C09 开发与批量投递测试留下的 49 条历史记录（状态均为 `DISPATCHED`）；仅 2 条由用户真实在抖音 App/Web 收藏的作品被实际拉入 D10 Worker 完整管线并成功落地正式资产。
7. **图集 BGM 契约澄清**：图集原始响应中含有 BGM 音乐流链接，但根据 D09/D07 资产规范，图集的核心主体是连续的 WebP 图像序列，BGM 属于可选音频流，不强制打包进图像目录，归档校验结果 100% 判定为 `valid=True`。
8. **历史夹具与真实资产区分**：根目录下 `video_6611417973221494020_detail.json` 和 `album_7169622286633274635_detail.json` 为离线开发单元测试夹具，真实 E2E 正式资产仅为 `archive/douyin/7681603850364521734` 与 `7682038498466993905`。
9. **F2 版本锁定澄清**：部分历史文档曾出现小版本笔误，当前工程唯一锁定的生产依赖版本为 `f2 == 0.0.1.7`。

---

# 35. Next Phase Plan: Milestone M3 (后续阶段规划建议 - 冻结未启动)

> [!WARNING]
> 本章节仅为下一阶段工作的候选架构设想（Candidate / Proposed），**所有模型与切片算法均为非正式冻结的候选提议，严禁在当前任务中创建任何代码、执行任何开发或擅自跨入 M3**。

### 35.1 Milestone M3 核心目标
**M3: Media Knowledge Integration** 的目标是将 M2 归档生成的规范化 Formal Local Asset，无缝对接给现有的 `local-video-knowledge` 多模态处理流水线：
```
[ M2 Formal Local Asset ] (archive/douyin/<platform_content_id>/)
            │
            ▼ (定义 Canonical Asset 接入适配器)
[ 音频提取与分轨 ] (候选格式: 16kHz 16-bit 单声道 WAV)
            │
            ▼
[ ASR 语音识别 ] ([CANDIDATE] 候选模型: faster-whisper large-v3，提取带精确时间戳的字幕文本)
            │
            ▼
[ 视觉场景分析与 OCR/VLM ] ([CANDIDATE] 候选方案: PaddleOCR 关键文字检测，Qwen-VL 视觉实体提取)
            │
            ▼
[ 证据层融合与知识条目抽取 ] (文本与画面互证，沉淀为知识单元)
```

### 35.2 M3 启动建议顺序 (待未来正式立项评估)
1. **任务 1**：编写 `CanonicalMediaAssetAdapter`，使已有流水线能够直接读取 `asset_manifest.json`。
2. **任务 2**：图集作品的多图像排版与 OCR/VLM 专属多模态处理管线接入方案评估。
3. **任务 3**：音视频长视频基于时间切片的启发式分块处理（[PROPOSED] 候选切片算法: Chunking v2.3）。
4. **任务 4**：打通原始收藏元数据（作者、标签、描述）与知识单元的溯源绑定。

---

# 36. Recommended Resume Sequence (数月后恢复项目的推荐顺序)

若未来在全新对话或由新 Agent 恢复本项目，请严格遵守以下 10 步标准化检查流程：

1. **第 1 步**：完整阅读本文档（`G:\antigravity-cli\dy\M2_DOUYIN_COMPLETE_HANDOFF.md`），建立全局认知。
2. **第 2 步**：检查 Git 状态（`git status`），注意工作树处于 DIRTY 状态属于正常 M2 留存，严禁擅自 reset 或 commit。
3. **第 3 步**：核实关键运行时环境与依赖路径是否存在（Python 3.12, F2 0.0.1.7, ffmpeg/ffprobe）。
4. **第 4 步**：执行只读数据库完整性检查，确认 `metadata.db` 与 `downloader_state.sqlite3` 未损坏（PRAGMA integrity_check）。
5. **第 5 步**：运行 `verify_archived_asset()` 校验已有 C10 归档资产，确认历史成果完整。
6. **第 6 步**：在主环境下运行离线集成回归测试（`pytest tests/test_collector_downloader_e2e.py -q`），确认全部 PASS。
7. **第 7 步**：检查 F2 Worker 环境（`G:\local_pc_project\.venv-f2\Scripts\python.exe -m pytest tests/test_downloader_worker.py -k "not live" -q`）。
8. **第 8 步**：仅在明确需要恢复采集时，运行一次只读认证探针（`python -m src.collector.cli douyin probe`）。
9. **第 9 步**：**切勿盲目启动 live sync 或大量下载！**
10. **第 10 步**：向用户明确请示：“当前 M2 状态健康完整，是否批准进入 Milestone M3 开发？”

---

# 37. Decision Log (关键技术决策与架构权衡)

| 决策项 (Architectural Decision) | 核心采纳原因 (Rationale) | 被否决的备选方案 (Rejected Alternative) |
| :--- | :--- | :--- |
| **浏览器上下文签名代理** | 彻底规避本地逆向动态签名高频失效与风控封号风险 | 纯 Python 本地逆向算法模拟请求（易被平台风控拦截封锁） |
| **F2 独立虚拟环境隔离** | F2 依赖库庞大且版本敏感，彻底隔绝主工程依赖冲突 | 在主环境中混合安装 F2（导致主项目依赖版本锁死与污染） |
| **SQLite 事务性 Outbox 模式** | 解耦采集发现与物理下载，防止网络下载卡顿阻塞增量采集 | 采集到作品直接同步阻塞下载（遇到大视频会导致同步超时卡死） |
| **双数据库物理隔离** | 职责权属清晰，Collector 与 Downloader 互不拥有对方写权限 | 单一全局大数据库（权限混淆，容易发生死锁与反向篡改） |
| **归档目录账号无关** | 跨账号重复收藏天然去重，物理文件只存一份 | 路径按 `scope_id` 划分（导致同一作品在不同账号下重复下载占用磁盘） |
| **`asset_manifest.json` 作为唯一提交承诺标志** | 确保原子发布，防止未完成的半成品文件被上层管线读取 | 仅靠检查 `.mp4` 文件是否存在（极易读到未下载完的损坏碎片） |
| **Worker 进程级单任务串行调度** | 保守风控，防止触发平台并发限制与 IP 封锁 | 激进的多任务多线程并发下载（极易引发 429 与验证码风暴） |
| **路由决策权归属 `content_type`** | 确定性强，根据元数据准确分流图集与视频逻辑 | 由下载器盲目探测文件格式（无法应对图集与图文混排场景） |
| **日常增量永远从 `cursor="0"` 探测** | 抖音收藏接口时序特征决定了最新收藏必定出现在第一页 | 从历史水位游标继续往后拉（会导致无法发现新收藏） |
| **原始 JSON 证据保留 (Raw Archive)** | 审计与法律合规保障，当业务转换规则变更时支持重新解析 | 仅落盘 Canonical 数据（原始响应丢失后无法纠错） |
| **Obsidian 绝不作为数据源真相** | 知识库笔记是展示与交互界面，工程数据库才是真实真相源 | 直接操作 Markdown 文件作为元数据仓储（并发冲突率高且不可靠） |

---

# 38. Current Golden Paths (绝对黄金路径有效性实测)

本文档涉及的所有核心路径均经过宿主机实际 `os.path.exists()` 实时物理核验：

| 逻辑标识 (Key) | 实际绝对路径 (Absolute Path) | 物理核验状态 |
| :--- | :--- | :---: |
| **Master Handoff Doc** | `G:\antigravity-cli\dy\M2_DOUYIN_COMPLETE_HANDOFF.md` | **`EXISTS`** |
| **Repo** | `G:\local_pc_project` | **`EXISTS`** |
| **Main venv** | `G:\local_pc_project\.venv` | **`EXISTS`** |
| **F2 venv** | `G:\local_pc_project\.venv-f2` | **`EXISTS`** |
| **Chrome profile** | `G:\antigravity-cli\dy\runtime\chrome-profile` | **`EXISTS`** |
| **Collector DB** | `G:\local_pc_project\data\metadata.db` | **`EXISTS`** |
| **Downloader DB** | `G:\local_pc_project\data\downloader_state.sqlite3` | **`EXISTS`** |
| **Raw archive** | `G:\local_pc_project\data\raw` | **`EXISTS`** |
| **Formal archive** | `G:\local_pc_project\archive` | **`EXISTS`** |
| **Sandbox root** | `G:\local_pc_project\data\sandbox` | **`EXISTS`** |
| **Docs root** | `G:\antigravity-cli\dy` | **`EXISTS`** |
| **Key config** | `G:\local_pc_project\config\config.json` | **`EXISTS`** |
| **C10 formal video** | `G:\local_pc_project\archive\douyin\7681603850364521734` | **`EXISTS`** |
| **C10 formal album** | `G:\local_pc_project\archive\douyin\7682038498466993905` | **`EXISTS`** |

---

# 39. Security Audit & No Secrets Confirmation (无凭证泄露审查确认)

本文档在生成前已经过严格的隐私与安全合规扫描：
- [x] **无 Cookie 明文**：未记录任何 `sessionid`, `sid_guard`, `passport_csrf_token` 的真实数值。
- [x] **无动态 Token**：未记录任何实时反爬签名（`msToken`, `a_bogus`）的真实数值。
- [x] **无带签 CDN 链接**：未记录带有效期签名的临时音视频下载流 URL（统一替换为 `[REDACTED_MEDIA_URL]`）。
- [x] **脱敏合规**：所有配置示例与日志示例均经过脱敏替换处理。

---

# 40. Final Validation Checklist (最终交付核对清单)

- [x] 1. 所有 14 个 Golden Paths 均已物理验证存在。
- [x] 2. 所有启动与停止命令均基于当前实际 CLI。
- [x] 3. Vikunja 任务状态与数据库已核验一致（20/20 DONE）。
- [x] 4. 测试回归基准准确（Main 672 pass / 4 skip，Worker 55 pass / 10 deselect）。
- [x] 5. 正式归档资产（视频与图集）物理参数与 SHA-256 强校验准确一致。
- [x] 6. 双数据库名称与表结构准确无误，对账释疑详实（51 outbox vs 2 jobs，标明 Legacy Exception）。
- [x] 7. 增量水位停机机制（C05）依据最新生产代码严格重写，纠正发布时间误区。
- [x] 8. 错误重试分类（D08）依据 `DownloaderErrorCode` 契约与 `ProductionDownloadErrorPolicy` 统一收敛。
- [x] 9. 全文无任何真实凭证泄露。
- [x] 10. Milestone M3 明确标注为 **FROZEN / NOT STARTED**，模型标注为候选提议。
- [x] 11. **FINAL STOP 约束维持，流水线处于完全安全停机状态**。
