# Dify RAG 工作流搭建手册

| 项目 | 保险条款智能问答助手 |
| --- | --- |
| 文档日期 | 2026-10-11 |
| 实测环境 | Dify 1.17.1 + weaviate 1.39.2 + Ollama + TEI(turing-1.2) |
| 硬件 | RTX 2080 Ti 魔改 22 GB |
| 适用范围 | 从零搭建到可被 Java 后端调用的 RAG 链路 |

> 本文所有配置值、分数、报错均为本机实测，不是照抄官方教程。
> 环境启动部分见另文 `环境启动手册.md`，本文只讲**工作流怎么搭、怎么验、踩过哪些坑**。

---

## 目录

1. [整体流程](#1-整体流程)
2. [前置检查清单](#2-前置检查清单)
3. [Step 1 · 安装模型供应商插件](#3-step-1--安装模型供应商插件)
4. [Step 2 · 创建知识库并上传文档](#4-step-2--创建知识库并上传文档)
5. [Step 3 · 配置知识库级检索策略](#5-step-3--配置知识库级检索策略)
6. [Step 4 · 编排 Chatflow 工作流](#6-step-4--编排-chatflow-工作流)
7. [Step 5 · 发布与 API Key](#7-step-5--发布与-api-key)
8. [Step 6 · 端到端验证](#8-step-6--端到端验证)
9. [踩坑记录（10 条）](#9-踩坑记录10-条)
10. [检索质量基线](#10-检索质量基线)
11. [辅助脚本](#11-辅助脚本)
12. [与需求文档的偏差](#12-与需求文档的偏差)

---

## 1. 整体流程

```
Step 1  装插件      Ollama ×2（LLM + Embedding）+ TEI（Rerank）
   ↓
Step 2  建知识库    高质量索引 → 分段设置 → 上传 2 份 PDF
   ↓
Step 3  检索设置    知识库级：混合检索 + 权重 + Rerank + 阈值 + TopK
   ↓
Step 4  编工作流    开始 → 知识检索 → LLM → 回答
   │                ⚠️ 节点级检索参数必须【重新配一遍】
   ↓
Step 5  发布        生成 API Key
   ↓
Step 6  验证        应答类 4 条 + 库外拒答类 4 条
```

**一句话记住最容易翻车的地方**：

> 知识库级和节点级检索参数是**替换关系，不是继承关系**。
> 只配知识库级，工作流里的阈值和 TopK 全部失效，而且**界面不报错**。

---

## 2. 前置检查清单

动手前先跑一遍，任何一项不过就先解决它。

```powershell
# ① Dify 是否健康
docker ps --format "{{.Names}}`t{{.Status}}" | Select-String "dify"
#    期望：15 个常驻容器（不含 init_permissions 一次性容器），api/postgres/sandbox 为 healthy

# ② 向量库类型（影响后面所有配置）
cd D:\project\private\new_life\local\dify\docker
Select-String -Path ".env" -Pattern "^(VECTOR_STORE|NGINX_PORT)"

# ③ LLM / Embedding 就位
Invoke-RestMethod http://127.0.0.1:11434/api/tags |
  ForEach-Object { $_.models | Select-Object name, size }

# ④ Rerank 就位（TEI 容器）
docker ps --filter "name=tei-rerank" --format "{{.Names}} {{.Status}}"
Invoke-WebRequest http://127.0.0.1:9999/health -TimeoutSec 10   # 期望 HTTP 200

# ⑤ 显存够用
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
#    期望 ~2.5–3 GB / 22528 MiB
```

**本机实测结果**：Dify 1.17.1、`VECTOR_STORE=weaviate`、Ollama 有 6 个模型、TEI 已就绪、显存 2992/22528 MiB。

---

## 3. Step 1 · 安装模型供应商插件

Dify 1.x 的模型供应商是**插件形态**，全新安装时一个都没有，必须先装。

### 3.1 安装

`Marketplace → 分类选「模型」→ 找到插件 → 点「安装」`

需要装两个：

| 插件 | 作者 | 安装量 | 用途 |
| --- | --- | --- | --- |
| **Ollama** | langgenius | 1,009,076 | LLM + Embedding |
| **Text Embedding Inference** | langgenius | 152,298 | Rerank |

### 3.2 配置（每个模型要单独建一条）

`设置 → 模型供应商 → 选插件 → 添加模型`

#### ① LLM

| 字段 | 值 |
| --- | --- |
| 模型名称 | `qwen3.5:9b` |
| 模型类型 | **LLM** |
| Base URL | `http://host.docker.internal:11434` |
| 模型模式 | **聊天** |

#### ② Embedding

| 字段 | 值 |
| --- | --- |
| 模型名称 | `bge-m3:latest` |
| 模型类型 | **Text Embedding** |
| Base URL | 同上 |

#### ③ Rerank

| 字段 | 值 |
| --- | --- |
| 模型名称 | `bge-reranker-v2-m3` |
| 模型类型 | **Rerank** |
| Base URL | `http://tei-rerank:80` |
| API Key | 留空 |

### 3.3 验证

```powershell
cd D:\project\private\new_life\local\dify\docker
docker exec docker-db_postgres-1 psql -U postgres -d dify -c \
  "select provider_name, model_name, model_type, is_valid from provider_models;"
```

期望三条全 `is_valid = t`：

```
 langgenius/ollama/ollama        | qwen3.5:9b         | llm             | t
 langgenius/ollama/ollama        | bge-m3:latest      | text-embedding  | t
 langgenius/huggingface_tei/...  | bge-reranker-v2-m3 | rerank          | t
```

> ✅ **TEI 插件确实支持 Rerank 类型**（此前存疑，已实测确认）。

---

## 4. Step 2 · 创建知识库并上传文档

`知识库 → 创建知识库`

### 4.1 索引方式

| 项 | 值 | 说明 |
| --- | --- | --- |
| 索引方式 | **高质量** | ⚠️ **一经创建不可切换为经济型，选错无法回退** |

只有高质量才会向量化，才会启用混合检索 / Rerank / 阈值。

### 4.2 分段设置

| 字段 | 值 | 理由 |
| --- | --- | --- |
| 分段模式 | **通用** | 条款是编号长文，父分段可作为备选 |
| 分段标识符 | **换行** | 按条款自然边界切 |
| 最大长度 | **500** | 保险条款单条通常不长 |
| 分段重叠 | **50**（约 10%） | 符合 FR-02 的 10%~20% |

> 条款有「第 X 条」天然结构，若召回质量不理想，可试**父分段（Parent-Child）**——大段做召回、小段做输入。这条留给 FR-09 评测集量化，不要拍脑袋。

### 4.3 上传文档

数据源（已通过 `docs/数据源验证报告.md` 验证均为电子文本，非扫描件）：

| 文件 | 险种 | 页数 |
| --- | --- | --- |
| `data\source\太保团体终身重大疾病保险E款条款.pdf` | 重疾 | 48 |
| `data\source\太保互联网个人一年期住院医疗保险条款.pdf` | 医疗 | 36 |

### 4.4 验证

```powershell
docker exec docker-db_postgres-1 psql -U postgres -d dify -c \
  "select name, indexing_status, word_count from documents;"
```

期望两行 `completed`，字数约 42997 / 56662。

再确认向量真的落库了（**这一步不能省**，`indexing_status=completed` 不代表向量存在）：

```powershell
docker exec docker-db_postgres-1 psql -U postgres -d dify -t -A -c \
  "select index_struct from datasets;"

# 输出：{"type":"weaviate","vector_store":{"class_prefix":"Vector_index_<dataset_id>_Node"}}

docker exec -e WKEY=<WEAVIATE_API_KEY> docker-worker-1 python -c "
import os,urllib.request,json
h={'Authorization':'Bearer '+os.environ['WKEY'],'Content-Type':'application/json'}
q='{ Aggregate { Vector_index_<dataset_id>_Node { meta { count } } } }'
r=urllib.request.Request('http://weaviate:8080/v1/graphql',
    data=json.dumps({'query':q}).encode(),headers=h,method='POST')
print(urllib.request.urlopen(r,timeout=20).read().decode())
"
```

本机实测：**3902 条向量**。

---

## 5. Step 3 · 配置知识库级检索策略

### 5.1 位置（Dify 1.17 与旧版不同）

```
知识库 → 打开你的知识库 → 左侧栏最下面的「设置」→ 往下滚到「检索设置」
```

> ⚠️ **旧版是在「文档」页右上角，1.17 挪到了侧栏的「设置」页签。**
> 左侧栏从上到下是：文档 / 流水线 / 召回测试 / **设置**。

左侧的「**召回测试**」页签可以脱离 LLM 直接看召回片段和分数，**调参阶段用它**，比建完工作流再来回试快得多。

### 5.2 配置值

| 配置项 | 值 |
| --- | --- |
| 检索设置 | **混合检索** |
| 权重设置 | 语义 **0.7** / 关键词 **0.3** |
| Rerank 模型 | **bge-reranker-v2-m3** |
| Score 阈值 | **0.5**（必须切到「启用」态） |
| TopK | **8** |

> 🔴 **Score 阈值默认是「关闭」的。** 建库后不去手动打开，它就是一个摆设——Dify 不会过滤任何低分片段。

### 5.3 验证

```powershell
docker exec docker-db_postgres-1 psql -U postgres -d dify -t -A -c \
  "select retrieval_model from datasets;"
```

期望包含：

```
"top_k": 8
"search_method": "hybrid_search"
"reranking_enable": true
"reranking_model": {"reranking_model_name": "bge-reranker-v2-m3", ...}
"score_threshold": 0.5          ← 不能是 null
"score_threshold_enabled": true ← 必须是 true
"vector_weight": 0.7, "keyword_weight": 0.3
```

同时确认 `vector_setting.embedding_model_name` 不是空字符串——这个字段空着的话混合检索的向量分支取不到配置。

---

## 6. Step 4 · 编排 Chatflow 工作流

`工作室 → 创建应用 → 聊天助手（Chatflow）`

> ⚠️ 别选成 Workflow 或 Agent。

### 6.1 节点结构

```
开始 ──▶ 知识检索 ──▶ LLM ──▶ 回答
```

**4 个节点、3 条连线。** 从模板创建时默认有 5 个节点（多一个「参数提取器」），必须删掉。

### 6.2 知识检索节点

| 字段 | 值 |
| --- | --- |
| 知识库 | **只选「保险条款」一个** |
| 查询变量 | `开始.sys.query` |
| 检索设置 | **与知识库级完全一致** |

节点级检索设置：

| 配置项 | 值 |
| --- | --- |
| 检索设置 | 混合检索 |
| 权重 | 语义 0.7 / 关键词 0.3 |
| Rerank 模型 | `bge-reranker-v2-m3` |
| Score 阈值 | **0.5**（不能留空！） |
| TopK | **8** |

> 🔴 **这是整个流程最容易错的地方。** 节点级配置**整体替换**知识库级，不叠加。
> 节点级 Score 阈值留空 = 运行时按 **0.0** 处理 = 不做任何过滤 = 兜底拒答失效。
> 详见 [§9.2](#92-节点级检索参数整体替换知识库级不是叠加)。

### 6.3 LLM 节点

| 字段 | 值 |
| --- | --- |
| 模型 | `qwen3.5:9b` |
| 上下文 | 开启，变量 = `知识检索.result` |
| 上下文记忆 | **关闭**（FR-05 单轮） |

提示词：

```
你是一名保险条款问答助手。请严格依据【检索到的条款内容】回答用户问题。

规则：
1. 只使用【检索到的条款内容】中的信息，不得用你自己的知识补充或推测。
2. 如果【检索到的条款内容】中没有回答问题所需的信息，请直接回复"未找到相关信息"，不要猜测、不要编造。
3. 引用条款时保留原文的数字、日期、金额、天数等关键信息，不要改写数值。
4. 答案末尾不要添加免责提示或任何额外说明。
5. 这是单轮问答，不需要追问或澄清。

【检索到的条款内容】
{{#context#}}

【用户问题】
{{#sys.query#}}
```

> 🔴 **规则 5 不能省。** Dify 模板默认提示词里写着「如果你在不确定的时候不知道，**就寻求澄清**」，
> 这与 FR-05「单轮、问『它的等待期呢』应被拒答」**直接冲突**——模型会反问「您指哪款产品？」，
> 既不是拒答，还凭空多一轮交互。

### 6.4 回答节点

- 输出变量：`{{#<LLM节点ID>.text#}}`

> **不需要开「引用」开关**——Dify 1.17 的 `AnswerNodeData` 只有 `answer` 一个字段，压根没有 citation 开关。
> `retriever_resources` 是知识检索节点**无条件输出**的，API 返回里一定带，Java 侧直接读即可。

### 6.5 验证

见 [§9 踩坑记录](#9-踩坑记录10-条) 中提供的一键校验：

```powershell
& "C:\Python312\python.exe" scripts\export_all_graphs.py
```

用 Dify 自身的 `KnowledgeRetrievalNodeData` 解析 `score_threshold`，
**必须被解析成 0.5 才算过**——写进 JSON 是 0.5 但运行时被兜成 0.0 的情况，只有这样验得出来。

---

## 7. Step 5 · 发布与 API Key

1. 打开应用 → 右上角 **「API 访问」**
2. 「API 密钥」区块 → **「创建 API Key」**
3. 命名（如 `rag-demo-java`）→ 确定
4. **立刻复制——Key 只显示这一次**

`is_public = false` 不影响 API 访问（该字段只管 Web 分享链接）。

### Java 侧接入

**Base URL**：`http://localhost/v1`
（`.env` 里 `APP_API_URL` 为空时，Dify 按请求 Host 自动推断）

**请求体**（`advanced-chat` 模式**必须**带 `inputs`，哪怕空对象）：

```json
{
  "inputs": {},
  "query": "住院医疗险的等待期是多久？",
  "response_mode": "streaming",
  "user": "session-user-id"
}
```

**鉴权**：`Authorization: Bearer app-xxx`

**引用来源**：`metadata.retriever_resources`，每条含
`document.name` / `position` / `score` / `segment.content` —— 即 FR-06 组装 Citation 的全部字段。

**配置**：

```properties
dify.api-key=${DIFY_API_KEY}      # 走环境变量，不要硬编码
dify.base-url=http://localhost/v1
dify.user-id-from=session
```

> API Key 的泄露风险与身份证号同级——它等于把整个问答链路敞开，
> 同样受需求文档 §4.3「日志中不落敏感值」约束。

---

## 8. Step 6 · 端到端验证

```powershell
& "C:\Python312\python.exe" scripts\test_chatflow_api.py --key "app-你的key"

# 测流式（对应 Java 侧 SSE，输出首 token 延迟）
& "C:\Python312\python.exe" scripts\test_chatflow_api.py --key "app-你的key" --stream
```

### 测试集

**应答类**（期望答出 + 命中关键数字）

| 问题 | 期望数字 |
| --- | --- |
| 投保后多久内身故赔钱？ | 90 日 |
| 住院医疗险的等待期是多久？ | 30 日 |
| 重疾险的犹豫期是多久？退保扣多少钱？ | 15 日 / 工本费 |
| 住院医疗保险的一般医疗免赔额是多少？ | 1 万元 |

**库外类**（期望全部拒答）

| 问题 |
| --- |
| 这款产品的年化收益率是多少？ |
| 怎么申请理赔，需要带什么材料？ |
| 这个保险能贷款吗？ |
| 公司电话是多少？ |

### 验收对照

| 验收项 | 要求 |
| --- | --- |
| AC-02 | 应答类覆盖率 ≥85%，关键数字无错 |
| AC-03 | 引用命中率 ≥90%，每条可回看原文 |
| AC-04 | 库外类全部拒答，应答类误拒率 ≤10% |
| 非功能 | 检索+Rerank ≤800ms；首 token ≤2s |

---

## 9. 踩坑记录（10 条）

### 9.1 Dify 1.17 的检索设置挪了位置

**现象**：按旧教程在「文档」页找「检索设置」，找不到。

**原因**：1.17 挪到了知识库左侧栏最下面的**「设置」**页签里。
左侧栏顺序：文档 / 流水线 / **召回测试** / **设置**。

---

### 9.2 节点级检索参数整体替换知识库级，不是叠加 🔴

**这是本项目最严重的一个坑。**

**现象**：知识库里阈值 0.5、TopK 8、Rerank 全配好了，工作流里也显示正常，
但问「这款产品的年化收益率是多少」，模型照样编一个答案出来。

**源码证据**（`api/core/workflow/nodes/knowledge_retrieval/knowledge_retrieval_node.py:279`）：

```python
top_k=node_data.multiple_retrieval_config.top_k,
score_threshold=node_data.multiple_retrieval_config.score_threshold
if node_data.multiple_retrieval_config.score_threshold is not None
else 0.0,          # ← 空值直接变 0.0
reranking_model=reranking_model,   # ← 节点里是 None 就传 None
weights=weights,                   # ← 节点里是 None 就传 None
```

节点构造整个 `KnowledgeRetrievalRequest`，**知识库级的设置一个都没传下去**。

**后果**：`score_threshold = 0.0` → 不做任何过滤 → 低分片段照进 LLM → **兜底拒答（FR-05）整条链路失效**。

**且界面不报任何错。**

**解法**：节点级检索参数必须**重新配一遍**，与知识库级保持一致。

**为什么这个坑特别危险**：Dify UI 里两处都有完整的检索设置面板，填完看着都对，
但「继承」这件事不成立。如果不读源码或做端到端验证，会一直以为是模型问题或提示词问题。

> Dify 1.17 的 `MultipleRetrievalConfig` 里**已经没有 `score_threshold_enabled` 字段了**，
> 只剩 `score_threshold: float | None`。所以建库时的三选一开关，在节点级对应的是一个可留空的数值框——
> 留空就是 0.0，等于关。

---

### 9.3 模板的「参数提取器」会把答案抽成空

**现象**：工作流跑完，前端**一片空白**，没有任何报错。

**原因**：从模板创建的应用默认带一个「参数提取器」节点（参数描述还是模板残留的「Dify的安装」），
回答节点的输出指向它：`{{#1719060889083.answer#}}`。
这个节点用 LLM 去抽一个叫 `answer` 的参数，抽不到就渲染成空字符串。

**三重伤害**：
1. **多跑一次完整 LLM 调用**，直接冲击 §4.1 的 3 秒预算
2. 有把答案改写或置空的风险
3. 纯冗余

**解法**：删掉该节点，LLM 直连回答，输出改成 `{{#<LLM节点ID>.text#}}`。

---

### 9.4 模板带了两个悬空的 dataset_id

**现象**：知识检索节点的 `dataset_ids` 有 3 个，但数据库里只有 1 个知识库。

```
6084ed3f-...  ← 数据库无此库（模板自带）
b0de9dad-...  ← 数据库无此库（模板自带）
b840bb7a-...  ← 保险条款 ✅
```

**解法**：知识库选择器里只保留真实存在的那个。

---

### 9.5 Base URL 不能写 127.0.0.1

**现象**：Ollama 供应商测试失败。

**原因**：Dify 跑在容器里，容器的 `127.0.0.1` 指向容器自己，不是宿主机。

**正确写法**：`http://host.docker.internal:11434`

**实测反证**（从 Dify 的 api 容器内部发起）：

```
[PASS] Ollama (via host.docker.internal)   6 个模型
[PASS] 反例 127.0.0.1（应当失败）           Connection refused
```

---

### 9.6 TEI 镜像标签没有 `-cuda-12.4` 这种写法

**现象**：

```
Error response from daemon: failed to resolve reference
"ghcr.io/huggingface/text-embeddings-inference:1.8.2-cuda-12.4": not found
```

**标签约定是 `{架构}-{版本}`**，可选架构前缀：`cpu` / `turing` / `86` / `89` / `hopper`。
最新版本 1.2.x。

本机是 Turing（sm_75），正确用 `turing-1.2`——HF 为 2080 Ti 这代卡单独出了构建版本，
不要用 `latest`（默认按 Ampere 及以上优化）。

---

### 9.7 直接改数据库会被 UI 的旧状态覆盖

**现象**：脚本已经把修复写进 draft，半小时后 draft 又变回修复前的内容。

**时间线**：

```
12:24  脚本写入修复
13:00  用户发布 -> 版本 2 是坏的
13:02  用户在未硬刷新的页面上保存 -> draft 被旧状态覆盖
```

**原因**：Dify 的工作流页面在打开时把图加载进前端内存。
**没有硬刷新就保存，会把修复前的旧图整个写回去。**

**教训**：
1. 脚本改完数据库，**必须让用户先 `Ctrl + Shift + R` 硬刷新**再动 UI
2. 或者**draft 和已发布版本一起改**——只改 draft 不影响正在跑的已发布版本

```powershell
& "C:\Python312\python.exe" scripts\fix_workflow_all.py --apply   # 两份都改
```

---

### 9.8 PowerShell 重定向会破坏 UTF-8 中文

**现象**：`docker exec ... psql > file.json` 导出的 JSON 解析失败：

```
json.decoder.JSONDecodeError: Expecting ',' delimiter: line 1 column 1083
```

实际内容：`"开�?` —— `始` 被截断成半个字符。

**原因**：PowerShell 的 `>` 会用控制台编码（中文 Windows 下通常 GBK）重新编码原生命令的 UTF-8 输出。

**解法**：全程走 subprocess 字节通道，不经过 PowerShell 文本管道：

```python
p = subprocess.run([...], capture_output=True)      # bytes
text = p.stdout.decode("utf-8")                     # 显式解码
```

> 注意：**导出损坏 ≠ 数据库内容损坏**。写入方向用的是 base64（ASCII 安全），不会中招。

---

### 9.9 TEI `/rerank` 返回裸数组

**现象**：解析 `rr["results"]` 报 `TypeError: list indices must be integers`。

**原因**：TEI 1.2 的 `/rerank` 返回：

```json
[{"index":0,"score":0.7369}, {"index":2,"score":0.6701}, ...]
```

不是 `{"results": [...]}`。

**解法**：两种都兼容——

```python
results = rr["results"] if isinstance(rr, dict) and "results" in rr else rr
```

---

### 9.10 `dataset_collection_bindings` 里的陈旧记录

**现象**：查这个表发现 collection 名是 `Vector_index_3b13d946_..._Node`，
但 weaviate 里实际只有 `Vector_index_b840bb7a_..._Node`，且**向量数正常（3902 条）**。

**结论**：**不影响检索**，是陈旧残留。

**权威来源是 `datasets.index_struct`**：

```json
{"type":"weaviate","vector_store":{"class_prefix":"Vector_index_b840bb7a_..._Node"}}
```

排查向量问题时应以它为准，不要看 `dataset_collection_bindings`。

---

## 10. 检索质量基线

以下为手动复现 Dify 检索链路的实测结果（bge-m3 → weaviate 向量召回 → TEI 重排 → 阈值 0.5）。

> ⚠️ 此基线只跑了**向量检索分支**，未跑 BM25 全文检索。
> Dify 的混合检索会合并两者，实际召回可能更好。

| 问题 | 类型 | 最高分 | 阈值 0.5 后剩 |
| --- | --- | --- | --- |
| 投保后多久内身故赔钱？ | 应答 | **0.8130** | 3 条 |
| 重疾险的犹豫期是多久？退保扣多少钱？ | 应答 | **0.8291** | 3 条 |
| 住院医疗险的等待期是多久？ | 应答 | **0.5935** | 2 条 |
| 这款产品的年化收益率是多少？ | **库外** | **0.0206** | **0 条 → 拒答** |

### 结论

**0.5 这个默认阈值落在一个巨大的空隙里**——应答类 0.59~0.83，库外类最高只有 0.0206。
它非常安全，甚至可以往上试 **0.7**。这组数据可直接作为 FR-09 评测报告的调参基线。

### 两个需要盯住的 case

**① 语义召回的正面证据**

「投保后多久内身故赔钱？」召回到的片段：

```
0.8130  …日为等待期，下同）内因意外伤害以外的原因导致身故，本公司按投保人…
0.5943  …之日起 90 日后因意外伤害以外的原因导致身故，本公司按以…
```

第 2 条正是**重疾险的「90 日」条款**——条款里根本没有「等待期」三个字
（见 `数据源验证报告.md` §4）。自然语言问法下语义召回直接命中，
**这是关键词检索必然漏召、向量检索能跨表述命中的直接证据**，也是很好的面试素材。

> 早前用三条测试片段测 TEI 时，这条只有 **0.0596** 分。
> 差异来自查询上下文长度与候选池大小——**说明单条测得的分数不能代表真实检索场景**，
> 基线必须在完整链路上跑。

**② 一个潜在问题**

「住院医疗险的等待期是多久？」召回到的前两条是：

> …指被保险人在我们指定医疗机构普通部**住院治疗前 30 日**（含住院当日）和**出院后 30 日**…

**不是**「自合同生效之日起 30 日为等待期」那条。阈值放行了它们，
但这两条**撑不起正确答案**。

两个可能原因：
1. 未跑 BM25，混合检索合并后「等待期」这个词面能捞回正确条款
2. 若合并后仍不对，需调高 TopK 或调权重

**这条正好是评测集里该重点盯的 case**——它考的正是「问法 ≠ 原文表述」。

### 关于分数可复现性（AC-07）

同一份输入、同一个模型，两次运行分数不完全相同：

| 片段 | 第 1 次 | 第 2 次 | 差 |
| --- | --- | --- | --- |
| 30 日等待期 | 0.7367796 | 0.7369689 | 2e-4 |
| 15 日犹豫期 | 0.6699608 | 0.67006874 | 1e-4 |
| 90 日责任条款 | 0.059646938 | 0.059319146 | 3e-4 |

CUDA 浮点归约顺序导致，正常现象。

⚠️ **AC-07 要求「同一参数下两次运行报告数值一致」**——
评测报告里的分数应统一保留 **2 位小数**，或明确写「容差 ±0.001」，
否则这条验收会假失败。

---

## 11. 辅助脚本

| 脚本 | 用途 |
| --- | --- |
| `reset_dify_password.py` | 忘记密码时重置（Dify 默认无 SMTP，邮件找回不可用） |
| `fix_workflow_all.py` | 修复 draft + 已发布版本两份图（`--apply` 才写库，默认预览 diff） |
| `export_all_graphs.py` | 导出所有版本 → 用 Dify 自身 schema 校验 |
| `verify_all_versions.py` | 容器内校验：11 项断言全过才算修好 |
| `retrieval_probe.py` | 手动复现检索链路，产出基线分数 |
| `test_chatflow_api.py` | 端到端验证（应答类 + 库外拒答类，含 retriever_resources） |

---

## 12. 与需求文档的偏差

| # | 位置 | 原文 | 实际 | 处理 |
| --- | --- | --- | --- | --- |
| 1 | §5.1 / §5.2 | 向量库 Qdrant | **weaviate 1.39.2** | 待回填 |
| 2 | §5.1 | Rerank `bge-reranker-v2-m3` | 一致 | 补充「由 TEI 容器提供，非 Ollama」 |
| 3 | §4.2 | `docker compose up` 拉起 `qdrant` | compose 内无 qdrant | 待回填 |
| 4 | §4.2 | 模型服务不部署在 Docker 内 | **Rerank 例外**（TEI 在容器内，轻量不与 ComfyUI 争抢显存） | 待回填 |
| 5 | FR-03 | 阈值 0.5 作为起点 | 实测落在 0.02~0.83 的空隙里，很安全，可试 0.7 | 补充实测数据 |
| 6 | FR-03 | 「两级串联筛选」 | **实际是替换关系**，节点级必须重配 | **必须补充**，否则 FR-03 描述有误导性 |
| 7 | FR-06 | 答案附引用出处 | `retriever_resources` 无条件输出，**无开关** | 补充说明 |
| 8 | AC-07 | 两次运行数值一致 | CUDA 下有 ~2e-4 抖动 | 分数保留 2 位小数或标容差 |

---

## 13. 一页速查

```
① 装 3 个模型插件         Ollama×2 + TEI×1，Base URL 绝不用 127.0.0.1
② 建知识库               索引=高质量（不可逆），重叠 50
③ 知识库级检索设置       左侧栏「设置」→ 混合检索 0.7/0.3 + Rerank + 阈值 + TopK
                         ⚠️ 阈值要手动切到「启用」
④ 编 Chatflow            删掉参数提取器，LLM 直连回答
⑤ 节点级检索设置         🔴 真正生效的那一档，必须配成与③一致
                         ⚠️ 留空=0.0=不过滤；只配③不配⑤等于没配（见 §9.2）
⑥ 提示词                 必须有「直接回复未找到相关信息」「不追问澄清」
⑦ 记忆变量               关闭（FR-05 单轮）
⑧ 发布 → 建 API Key      只显示一次，走环境变量
⑨ 验证                   4 条应答类 + 4 条库外拒答类
```

---

*文档结束*
