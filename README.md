# 保险条款智能问答助手 — 代码说明

RAG 链路由 **Dify** 编排，本仓库的 Java 侧是**治理层**（BFF + 审计 + 过滤 + 引用组装 + 评测），
前端是对话界面。整体架构与决策依据见 `docs/`。

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
├── eval/
│   ├── eval_cases.json          评测集 23 条
│   └── run_eval.py              批量评测，产出 eval-report.md
├── data/source/                 已验证的条款 PDF + 清洗后文本
├── docs/                        需求文档 V1.2、实测报告、数据源验证报告
└── scripts/
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

⚠️ 两份条款**必须用同一个 Embedding 模型**，否则 Dify 的权重设置不可用。

⚠️ 应用必须是 **Chatflow**（因为要挂问题分类器实现产品路由），
所以 API 端点是 `/v1/chatflow/chat-messages`，不是 `/v1/chat-messages`。

---

## 评测

```powershell
$env:DIFY_APP_API_KEY="app-xxxx"
# 重启后端后：
& "C:\Python312\python.exe" eval\run_eval.py --base http://localhost:8080 --mode blocking
```

产出 `eval/eval-report.md`，含五项验收线：

| 指标 | 验收线 | 对应 |
| --- | --- | --- |
| 应答类命中率 | ≥85% | AC-02 |
| 拒答类正确率 | ≥85% | AC-04 |
| 跨产品题 | ≥90% | AC-09 |
| 口语问法 | ≥75% | AC-10 |
| 引用命中率 | ≥90% | AC-03 |

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

---

## 当前状态

| 部分 | 状态 |
| --- | --- |
| 环境（Dify 1.17.1 / TEI rerank / Ollama / JDK 17） | ✅ 就绪 |
| 数据源（2 份 PDF，已清洗） | ✅ 就绪并实测可提取 |
| Java 治理层 | ✅ 编译通过，14 项单测全绿 |
| 治理层端到端验证 | ✅ 13/13 通过 |
| 前端 | ✅ 构建通过（146 KB / gzip 48 KB） |
| 评测集 + 脚本 | ✅ 已就绪（待 Dify 配好后首次跑） |
| **Dify 模型配置 + 双知识库 + 分类器路由** | ⏳ **待办**（需 Web UI 操作） |