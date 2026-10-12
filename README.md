# 保险条款智能问答助手 — 代码说明

RAG 链路由 **Dify** 编排，本仓库的 Java 侧是**治理层**（BFF + 审计 + 过滤 + 引用组装 + 评测），
前端是对话界面。整体架构与决策依据见 `docs/`。

---

## 架构图

图源在 `docs/archify/`（Archify 生成的可交互页面）。下面两张是静态快照，会跟随
你的浅色 / 深色主题切换；想点节点看依赖、或导出演示动效，打开 HTML 交互版。

### 整体架构

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/archify/architecture.visual-check.2048x1320.dark.png">
  <img alt="整体架构：营销端提问 → 前端 → Java 治理层 → Dify ChatFlow；Ollama 与 TEI Rerank 在容器侧，H2/PostgreSQL 审计库旁挂" src="docs/archify/architecture.visual-check.2048x1320.light.png">
</picture>

交互版：[`docs/archify/architecture.html`](docs/archify/architecture.html)

### 问答主链路

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/archify/chat-flow.visual-check.2048x1320.dark.png">
  <img alt="问答主链路：前端交互 → 治理层 SSE 编排 → Dify ChatFlow 知识检索与 LLM 生成 → 审计与知识库；含入参过滤、合约拦截、引用组装" src="docs/archify/chat-flow.visual-check.2048x1320.light.png">
</picture>

交互版：[`docs/archify/chat-flow.html`](docs/archify/chat-flow.html)

---

## 目录结构

```
RAG_demo/
├── backend/                     Java 17 + Spring Boot 3.3 治理层
│   └── src/main/java/com/insurance/rag/
│       ├── config/DifyProperties        Dify 配置（密钥只存服务端）
│       ├── dify/DifyClient              Chatflow SSE 客户端
│       ├── dify/DifyEvents              Dify 事件与 metadata 映射
│       ├── filter/SensitiveDataFilter   FR-08 敏感过滤
│       ├── citation/Citation            FR-06 引用模型
│       ├── citation/CitationAssembler   metadata → 结构化引用
│       ├── chat/ChatService             问答编排（过滤→Dify→组装→落库）
│       ├── domain/Conversation          FR-07 审计记录
│       └── repository/                  JPA 仓储
├── frontend/                    React + Vite 对话界面（流式 + 溯源回看）
├── eval/                        评测（FR-09）
│   ├── eval_cases_v2.json       评测集 52 条（v0 的 23 条 + 新设计 29 条）
│   ├── run_iteration.py         闭环评测器：前端取数 → 逐条落盘 → 失败归因 → 跨轮对比
│   ├── run_eval.py              v0 批量评测（保留）
│   ├── prompts/                 提示词版本
│   └── runs/<tag>/              每轮结果留档（raw.jsonl / report.md / summary.json）
├── data/source/                 已验证的条款 PDF + 清洗后文本
├── docs/                        需求文档 V1.2、实测报告、迭代报告、启动手册
│   └── archify/                 架构图源文件（可交互 HTML + 浅/深色快照 PNG）
└── scripts/
    ├── dify_params.py           Dify 参数读写 + 快照/回滚（node. 前缀区分作用域）
    ├── sweep_retrieval.py       参数扫描（只检索不调 LLM，一组 8 秒）
    ├── reset_dify_password.py   Dify 密码重置（你写的）
    └── verify_governance.py     治理层端到端验证（13 项断言）
```

---

## 运行

### 1. 后端

**方式 A（推荐 · 分离进程）** —— 服务脱离终端生命周期，关掉终端也在跑：

```powershell
# 首次先打包
cd backend
$env:JAVA_HOME="C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot"
$env:Path="$env:JAVA_HOME\bin;D:\tool\maven\apache-maven-3.9.9\bin;$env:Path"
mvn -DskipTests package

# 启动（脚本会等待端口就绪并打印 PID，实测 ~13 秒）
powershell -ExecutionPolicy Bypass -File scripts\start-backend.ps1
```

日志在 `backend/backend.log`；停止用脚本打印的 `Stop-Process -Id <PID>`。

**方式 B（最省事）** —— 在 IDE 里直接运行 `InsuranceRagApplication`，
或 `cd backend && java -jar target\insurance-rag-backend-0.1.0.jar`。

> ⚠️ 不要用 `mvn spring-boot:run` 长时间挂在自动化终端里——
> 常驻进程会被执行器的超时机制杀掉（实测 900 秒后被终止）。

#### 启动脚本踩过的坑（都写在 `start-backend.ps1` 注释里）

| 坑 | 现象 | 修法 |
| --- | --- | --- |
| `powershell` 是 **Windows PowerShell 5.1**，把无 BOM 的 UTF-8 按 GBK 解析 | 脚本中文乱码、**ParserError 且报错行号对不上** | 文件存为 **UTF-8 带 BOM** |
| `Start-Process -RedirectStandard*` | java 继承 PowerShell 管道句柄，脚本退不出被超时杀 | 不重定向，日志交给 Spring 自己写（`logging.file.name`） |
| CIM/WMI 进程工作目录是 `C:\Windows\System32` | H2 相对路径落到 System32：`Error while creating file ""C:/Windows/System32/data""` | 用 `-Dapp.data.dir=<绝对路径>`（`-D` 在 `-jar` 前，不含分号，不会被 cmd 引号规则吃掉） |
| 反引号续行符 | 续行静默失效，`-ArgumentList` 被当成独立命令 | 参数用数组传，不用续行符 |

默认用 H2 文件库（`backend/data/`），**不需要起数据库容器**即可跑通全流程。
切 PostgreSQL 只需改 `application.yml` 的 datasource 三行。

**密钥通过环境变量注入，不入库：**

```powershell
$env:DIFY_APP_API_KEY="app-xxxx"
$env:DIFY_DATASET_KEY_CRITICAL="dataset-xxxx"
$env:DIFY_DATASET_KEY_MEDICAL="dataset-xxxx"
```

### 2. 前端

```powershell
cd frontend
npm install
npm run dev        # http://localhost:5173，已配 /api 代理到 8080
```

### 3. 验证治理层（不依赖 Dify）

```powershell
& "C:\Python312\python.exe" scripts\verify_governance.py
```

13 项断言，覆盖入参过滤、脱敏落库、拒答降级、审计完整性。
**Dify 没配好也能跑**——治理层必须独立可验。

---

## Dify 侧配置（需在 Web UI 操作）

| 用途 | Provider | 参数 |
| --- | --- | --- |
| LLM | Ollama | `http://host.docker.internal:11434` + `qwen3.5:9b` |
| Embedding | Ollama | 同上 + `bge-m3:latest` |
| Rerank | Text Embedding Inference | `http://tei-rerank:80` |

当前检索参数（已调优定档）：

| 参数 | 值 | 在哪配 |
| --- | --- | --- |
| `score_threshold` | **0.1** | **检索节点** `multiple_retrieval_config`（知识库级同步改） |
| `top_k` | **30** | 同上。⚠️ **33 以上会静默返回空集** |
| 向量 / 关键词权重 | 0.7 / 0.3 | 知识库级 `datasets.retrieval_model`（实测调了没用） |
| `temperature` / `think` | 0.1 / false | LLM 节点 `model.completion_params` |

⚠️ **三份条款**（个人住院医疗 / 团体百万医疗 / 重疾E）**必须用同一个 Embedding 模型**，
否则 Dify 的权重设置不可用。

⚠️ 应用是 **Chatflow**，API 端点 `/v1/chatflow/chat-messages`。

🔴 **目前只有一个知识库，三份条款混在一起**，检索前没有产品约束——
这是跨产品串档（4/42）和同名章节错配（3/42）的根因。
拆双知识库 + 问题分类器路由是下一阶段的首要任务。
在拆库之前，**产品维度的准确率没有解**。

---

## 评测

评测集 52 条（`eval/eval_cases_v2.json`），取数走前端同款链路
（`POST /api/chat/stream`，SSE 解析与 `frontend/src/App.jsx` 逐行一致），
每条结果**即时落盘**到 `eval/runs/<tag>/raw.jsonl`。

```powershell
# 单轮全量评测（52 条约 15 分钟；--workers 并发在本机无效，Ollama 串行处理）
& "C:\Python312\python.exe" eval\run_iteration.py --tag r001 --workers 2

# 中断续跑：已跑过的不重测。**判据改了也用它免费重判，不必重新发请求**
& "C:\Python312\python.exe" eval\run_iteration.py --tag r001 --resume

# 跨轮对比
& "C:\Python312\python.exe" eval\run_iteration.py --compare r001_baseline r003_node010

# 看所有轮次概览
& "C:\Python312\python.exe" eval\run_iteration.py --list-runs
```

失败不是只记「过/不过」，而是**归因**并按危险度排序：

| 归因 | 含义 | 危险度 |
| --- | --- | --- |
| `wrong_product` | 引用里只有错误产品的文档 | 🔴 危 |
| `clause_mismatch` | 召回了同名章节，但编号/文档对不上 | 🔴 危 |
| `citation_miss` | 答了但引用没兜住期望条款 | 🔴 危 |
| `under_refusal` | 该拒却给了引用答案 | 🔴 危 |
| `over_refusal` | 该答却拒答 | 🟠 重 |
| `coverage_gap` | 引用对、答了，但关键信息点缺失 | 🟡 轻 |

### 调参：先扫后验，不要一上来就跑全量

全量评测一轮 15 分钟，扫参数却要几十上百组。Dify 的
`/v1/datasets/{id}/retrieve` **只检索不调 LLM**，一组 8 秒，用它先扫：

```powershell
& "C:\Python312\python.exe" scripts\sweep_retrieval.py --stage threshold   # 阈值
& "C:\Python312\python.exe" scripts\sweep_retrieval.py --stage knn         # top_k / 权重
```

命中入选后，再用全量评测验证。⚠️ **改阈值必须改节点级**——见 `docs/项目启动手册.md` §6.5。

### 验收线与当前实测

| 指标 | 验收线 | 当前实测 |
| --- | --- | --- |
| 应答类命中率 | ≥85% | 48% ❌ |
| 拒答类正确率 | ≥85% | 90% ✅ |
| 跨产品题 | ≥90% | 50% ❌ |
| 口语问法 | ≥75% | 36% ❌ |
| 引用命中率 | ≥90% | 65% ❌ |

**剩余失败不是参数问题。** 42 条应答题里 4 条串档、3 条同名章节错配，
根因是三份条款塞在**一个知识库、检索前没有产品约束**——
按 `docs/链路实测发现报告.md` 的建议，下一步是拆双知识库 + 问题分类器路由。

完整四轮迭代过程见 **`docs/评测迭代报告.md`**。

---

## 几个不显然的实现决定

1. **SSE 用 fetch 而不是 EventSource**：EventSource 只支持 GET，问答是 POST 带 body。
   解析时必须用 `TextDecoder` 流式模式——分帧边界可能落在多字节字符中间。

2. **敏感过滤的正则不用 `\b`**：Java 正则的 `\w` 只有 `[a-zA-Z_0-9]`，
   中文全算「非单词字符」，实测「我的身份证是110101199003078515」用
   `\b(\d{17}[\dXx])\b` **匹配失败**。改用 `(?<!\d)` / `(?!\d)`，
   语义是「前后不是数字」，既完整命中又不会从更长数字串里截取。

3. **Dify 不可用时必须落审计再降级**：否则审计链路会在故障时静默失效——
   恰恰是最需要留痕的时候没有留痕。

4. **过滤器的不误伤用例和漏检用例同等重要**：把「等待期 30 日」当成证件号
   会直接毁掉问答功能，所以单测里专门有一组 `doesNotMaskShortNumbers`。

5. **引用去重按 segmentId**：同一条片段可能被多条检索路径召回，
   不去重前端引用列表会出现重复项。

6. **SSE 的 `data:` 后面没有空格，但客户端按规范剥掉了一个**：
   Spring 写的是 `data:  **`（冒号后直接跟载荷），而 SSE 规范要求客户端
   剥掉 `data:` 后的**一个**前导空格，于是**每个以空格开头的增量都会丢掉那个空格**。
   实测 52 条里 38 条的流式拼接结果与最终答案对不上（`1. **X**` vs `1.  **X**`）。
   中文里多数是观感问题，但**一旦某个增量块里有换行且换行后有实义内容，那部分会被静默吞掉**。
   修法二选一：服务端保证 `data:` 后带空格，或客户端改成不剥。

7. **`top_k ≥ 33` 会静默返回空集**：HTTP 200、0 条记录、零异常。
   排查「突然全答不出」时先看这一项。

---

## 当前状态

| 部分 | 状态 |
| --- | --- |
| 环境（Dify 1.17.1 / TEI rerank / Ollama / JDK 17） | ✅ 就绪 |
| 数据源（**3** 份 PDF，已清洗） | ✅ 就绪并实测可提取 |
| Java 治理层 | ✅ 编译通过，14 项单测全绿 |
| 治理层端到端验证 | ✅ 13/13 通过 |
| 前端 | ✅ 构建通过（146 KB / gzip 48 KB） |
| Dify 模型配置 + 知识库入库 + Chatflow | ✅ 已配好并跑通（**单知识库，含 3 份条款**） |
| 评测集 + 闭环评测器 | ✅ **已跑 4 轮全量**，结果留档在 `eval/runs/` |
| 检索参数调优（阈值 / top_k / 权重） | ✅ 已扫完并定档，**已到参数天花板** |
| **产品路由（拆双知识库 + 问题分类器）** | ⏳ **待办** —— 当前串档类失败的唯一解 |
| **清理知识库里的目录/封面页** | ⏳ **待办** —— 52 条里 30% 的查询被目录页抢占高分位 |