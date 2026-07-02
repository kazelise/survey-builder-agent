# Survey Builder Agent

**一行自然语言 → 发布完整问卷**。将研究者的模糊指令转化为 cs14 平台上的已发布问卷，通过多轮函数调用驱动后端 REST API。

## 项目定位

### 核心流程
```
研究者输入（中/英）
  ↓
Agent 解析意图（语言、A/B 分组、问卷风格等）
  ↓
多轮工具调用构建问卷链（create → posts → questions → publish）
  ↓
返回分享链接 /survey/{share_code}?lang=…
```

### 项目特性
- **独立 uv 项目**：仅通过 HTTP 调用 cs14 后端，从不导入 backend 代码
- **无框架**：使用 Anthropic Python SDK 和 `httpx`，可配置 `base_url` + `model` 支持兼容端点
- **极简工具层**：13 个精准工具（12 个平台 API 工具 + 1 个 RAG 手册检索），短系统提示，单一显式循环（~120 行代码）
- **--mock 模式**：无 API key 也能运行，用脚本重放模型决策，工具层仍调真实 HTTP（CI/测试友好）
- **完整追踪**：每次运行生成 JSONL 文件，记录每一轮的模型输入、工具调用、令牌使用、成本估算
- **健壮的失败处理**：HTTP 重试 + 指数退避、工具结果截断、上下文窗口修剪、模型侧降级路径
- **MCP 服务器**：同一工具定义，可暴露给任何 MCP 主机（Claude Desktop、其他 Agent）
- **双层评估框架**：工具序列匹配 + 终态验证，支持 mock 和真实模型

---

## 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLI Entry Point                          │
│  (--mock, --dry-run, --base-url, --model, --trace, --max-turns) │
└────────────────────┬────────────────────────────────────────────┘
                     │
         ┌───────────┴───────────┐
         │                       │
    ┌────▼──────────┐      ┌─────▼─────────┐
    │  Config       │      │  Model        │
    │  (Settings)   │      │  Protocol     │
    └────┬──────────┘      └─────┬─────────┘
         │                       │
         │                   ┌───┴────────────┐
         │                   │                │
         │              ┌────▼─────┐    ┌────▼──────┐
         │              │ RealModel │    │ MockModel │
         │              │ (SDK)     │    │ (Script)  │
         │              └──────────┘    └───────────┘
         │
    ┌────▼──────────────────────────────────────┐
    │  Agent Loop                               │
    │  (turn counter, message history, stops)   │
    └────────────────┬─────────────────────────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
    ┌───▼────┐  ┌────▼────┐  ┌───▼────────┐
    │ Trace  │  │Executor  │  │ Context    │
    │(JSONL) │  │(dispatch)│  │ (state)    │
    └────────┘  └────┬─────┘  └────────────┘
                     │
        ┌────────────▼────────────┐
        │  Tool Handlers          │
        │  (13 tools)             │
        │  ├─ survey.py           │
        │  ├─ content.py          │
        │  ├─ auth.py             │
        │  └─ handbook.py (RAG)   │
        └────────────┬────────────┘
                     │
             ┌───────▼────────┐
             │ HTTP Client    │
             │ (retry+backoff)│
             └────────┬───────┘
                      │
              ┌───────▼───────┐
              │ CS14 Backend  │
              │ (REST API)    │
              └───────────────┘

横切关系：
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ MCP Server   │    │ Evals/       │    │ Tests        │
│ (FastMCP)    │    │ Runner       │    │ (pytest)     │
└──────────────┘    └──────────────┘    └──────────────┘
   共享 TOOLS schema，共享 Handlers，共享 HTTP 层
```

---

## 快速开始

### 安装与配置

```bash
cd agent
uv sync
cp .env.example .env   # 填入 ANTHROPIC_API_KEY / CS14_* 等
```

### 三种运行模式

#### 1. 完全离线（无 API key，无后端）
```bash
# 最简smoke test：不需要任何凭证，回放内置演示脚本，模型决策 mock，后端响应 stub
uv run survey-agent "" --mock --dry-run
```

#### 2. 离线模型，真实后端
```bash
# 需要 docker compose up -d 或 uvicorn 在 CS14_BASE_URL 上运行
uv run survey-agent "" --mock --trace traces/mock1.jsonl
```

#### 3. 真实模型，真实后端
```bash
# 需要 ANTHROPIC_API_KEY 或 `ant auth login` 验证
uv run survey-agent "帮我建一个小红书风格的中英双语问卷，A/B两组，两个帖子加一个李克特量表，发布并给我链接" \
  --model claude-opus-4-8 --trace traces/run1.jsonl
```

#### 4. 指向兼容端点/代理
```bash
uv run survey-agent "..." --base-url https://my-proxy/v1 --model some-model
```

### CLI 标志说明

| 标志 | 说明 |
|---|---|
| `instruction` (位置) | 自然语言构建请求（中/英）。空字符串在 `--mock` 模式下有效。 |
| `--mock [SCRIPT_JSON]` | 重放脚本化模型决策而非调用 API。无参数 = 内置双语 A/B 演示脚本。带路径 = 加载 `{"mock_script": [...]}` 或裸列表。 |
| `--base-url` | 覆盖 `ANTHROPIC_BASE_URL`。 |
| `--cs14-base-url` | 覆盖 `CS14_BASE_URL`。 |
| `--model` | 覆盖模型 ID。 |
| `--trace PATH` | JSONL 追踪输出（默认 `traces/<ts>.jsonl`）。 |
| `--max-turns N` | 循环轮次预算（默认 20）。 |
| `--lang` | 首选 `default_language` 提示，附加到指令。 |
| `--dry-run` | 跳过网络：`CS14Client` 返回合成响应。与 `--mock` 组合以获得零依赖 smoke test。 |

### 环境变量

参见 `.env.example`：
- `ANTHROPIC_API_KEY` — Anthropic API 密钥（或 `ant auth login`）
- `ANTHROPIC_BASE_URL` — 覆盖 API 端点（默认 `https://api.anthropic.com/v1`）
- `MODEL` — 默认模型 ID（默认 `claude-opus-4-8`）
- `CS14_BASE_URL` — cs14 后端 API 根（默认 `http://localhost:8000/api/v1`，注意必须带 `/api/v1` 前缀）
- `CS14_EMAIL` — 研究者邮箱（默认 `cs14.demo@example.com`）
- `CS14_PASSWORD` — 研究者密码（默认 `demo_password`）

---

## 设计决策五问五答（面向面试官）

### 问 1：为什么不用 Agentic Frameworks（如 LangChain、Crew.ai、Vercel AI SDK）？

**答：**
Agent loop 只需 ~120 行代码，自己写比用框架更清晰、更容易测试、更容易在面试中讲清楚。

1. **显式性**：循环逻辑在 `loop.py` 中一览无余，每一步的决策和停止条件都能讲出来。框架隐藏控制流，面试时难以深入讨论。
2. **可测试性**：因为循环依赖 `Model` 协议（不是直接导入 `anthropic`），所以可以零网络、零 API key 运行完整的端到端测试（用 `MockModel` 和 `httpx.MockTransport`）。
3. **可观测性**：`--mock` 模式和追踪（JSONL）是自己设计的，框架会限制粒度。
4. **成本**：框架通常过度设计（支持多种 LLM、多种工具类型、记忆管理等），而这个项目只需 Anthropic SDK + `httpx`。

**权衡**：如果项目跨越 10+ 个 LLM 供应商或需要长期记忆管理，框架才有价值。这里不需要。

---

### 问 2：为什么核心工具是 12 个、总共 13 个？工具怎么选、怎么分组？

**答：**

工具数量是根据 **后端 API 拓扑** 和 **问卷构建的幺正顺序** 导出的，而非武断决定。

**后端端点 → 工具映射**：
- `POST /surveys` → `create_survey`（一次锁定语言、A/B 分组、风格）
- `PATCH /surveys/{id}` → `update_survey`（改草稿字段）
- `GET /surveys/{id}`, `GET /surveys` → `get_survey`, `list_surveys`
- `POST /surveys/{id}/posts` → `add_post`（添加刺激材料）
- `PATCH /surveys/{id}/posts/{pid}` → `update_post_display`（mock/造假内容）
- `POST /surveys/{id}/posts/{pid}/comments` → `add_comment`
- `POST /surveys/{id}/posts/{pid}/questions`, `POST /surveys/{id}/questions` → `add_post_question`, `add_survey_question`
- `POST /surveys/{id}/publish` → `publish_survey`（终态）
- `GET /surveys/{id}/posts` → `list_posts`
- 本地生成 → `get_share_link`（构造最终分享链接）
- 本地检索 → `search_handbook`（RAG：BM25 + bge-m3 混合检索平台手册，带来源引用，见问 2b）

**分组原则**：
1. **4 个工具模块**：`tools/survey.py`（问卷元数据）、`tools/content.py`（内容构建）、`tools/auth.py`（引导）、`tools/handbook.py`（RAG 手册检索）。
2. **每个工具的 schema 都是严格的**：`additionalProperties:false`，枚举值固定（平台风格、问题类型、语言代码），让 API 错误成为编译期（handler 预检），不是运行期。
3. **为什么不更多**：翻译导入导出、分析、关闭/重开都在核心构建链外，暴露它们会稀释模型的工具选择精度。可后续在 `--advanced` 标志后面加。

---

### 问 2b：`search_handbook` 的 RAG 检索是怎么实现的？

**答：**

延续"无框架"原则，检索层零第三方依赖（不用 rank_bm25 / faiss / numpy）：

- **语料与索引**：`scripts/build_handbook_index.py` 把 `docs/*.md` 与 `docs-site/docs/**/*.md` 按标题层级切成约 193 个 chunk，落盘 `data/handbook_index.json`（入库）；BM25 统计在加载时于内存重建（几百个短 chunk 的分词是微秒级，不值得持久化倒排状态）。
- **双路检索 + RRF 融合**：BM25（纯 Python 实现）永远可用；本地 Ollama `bge-m3` embedding 可达时走 hybrid，两路结果用 Reciprocal Rank Fusion 融合——只比较名次不比较分数，避免 BM25 分数与余弦相似度量纲不可比的问题。
- **优雅降级**：embedding 索引未构建 / Ollama 不可达 / embedding 调用中途失败，一律降级为 BM25-only 并在工具结果里带 `note` 说明原因——检索工具绝不因语义路不可用而失败。
- **来源引用**：每条结果返回 `source_file + heading + snippet + score`，模型回答平台问题时可标注出处（系统提示第 6 条要求先查手册再回答，不许瞎猜）。
- **embedding 文件不入库**：`data/handbook_embeddings.json` 是环境依赖的派生产物（本机 Ollama 生成，`--embed` 重建），已 gitignore；入库的 BM25 索引才是"在任何机器上都能跑"的保底路径。

---

### 问 3：消息历史与上下文窗口怎么管理？

**答：**

**挑战**：多轮 tool-use 循环会快速填满上下文窗口，特别是 `list_posts` 等读操作返回大量 JSON。Opus 4.8 是 1M 令牌，但 HTTP 超时和成本约束实际预算是 120K。

**解决方案（分层）**：

1. **工具结果截断**（`executor.py`）：
   - 每个 `tool_result` 字符串限制在 4000 字符。
   - 超大结果（如返回 50 个帖子的 `list_posts`）被摘要为 `{count: 50, first_n_ids: [...], truncated: true}`。
   - 模型看到"有 50 个帖子但我只显示前 5 个"的信号，避免盲目重复读取。

2. **消息历史修剪**（`loop.py` 的 `trim_context()`）：
   - 每轮 `complete()` 前，检查 `messages` 的估算令牌数。
   - 若超过 `CONTEXT_BUDGET`（默认 120K），删除最旧的 **已完成** 的 tool_use/tool_result 对（永不孤立 ID）。
   - 保留原始用户指令和最新 N 轮的对话。

3. **有状态的 RunContext**：
   - 删除消息后，问卷状态（`survey_id`, `share_code`, `posts` 列表、`questions` 列表）保存在 `RunContext` 中。
   - 模型不需要从消息历史中恢复状态，可以安全地删除旧轮。

4. **系统提示缓存**（future）：
   - Opus 4.8 支持提示缓存（5 分钟 TTL）。工具列表稳定后，冻结的系统前缀可被缓存，省下重复的写成本。

**权衡**：修剪牺牲了精细回溯能力（模型看不到 15 轮前的决策），但 JSONL 追踪记录了完整历史，保留了调试能力。

---

### 问 4：怎么评估 Agent？哪些指标才能证明它工作？

**答：**

**双层评估框架**（见 `evals/`）：

1. **第一层：工具序列匹配**
   - 对每个测试用例，规定 "正确的工具序列" 是什么（e.g. `[create_survey, add_post, update_post_display, add_post, update_post_display, add_survey_question, publish_survey]`）。
   - 用 LCS（最长公共子序列）比对实际序列与期望序列，允许额外的 read 工具（`get_survey`, `list_posts`），但惩罚缺失或乱序的 mutations（e.g. publish 前没有 add_post = 失败）。
   - 给出相似度分数（0.0 ~ 1.0）。

2. **第二层：终态验证**
   - 在 publish 后调用 `get_survey`，检查：
     - `status == "published"`
     - `supported_languages` 包含期望的语言
     - `num_groups` 匹配 A/B 分组
     - 帖子计数、问题类型（e.g. 包含 likert）
   - 终态必须通过才算真正成功（工具序列对也是必要的，但不充分）。

3. **两种运行模式**：
   - **Mock（CI）**：脚本重放模型决策，100% 确定，看工具序列是否稳定。
   - **Real**：真实模型，每个用例独立运行多次，报告通过率、平均轮次、成本。

**指标**：
- 序列匹配分数 ≥ 0.9（允许小偏差）
- 终态通过率 ≥ 0.95（99% 成功）
- 平均轮次 ≤ 12（预算 20，说明模型有方向感）
- 估算成本（Opus 4.8）≤ $0.05 per run

**为什么这样设计**：
- 序列匹配检查 "逻辑对了没"。
- 终态检查 "结果对了没"。
- Mock 模式让 CI 快速反馈，real 模式承载长期精度指标。

---

### 问 5：什么时候会失败，怎么兜底？

**答：**

**五类失败及对策**：

#### 5.1 HTTP 网络错误（临时）
- **症状**：connection reset, timeout, 5xx
- **兜底**：指数退避重试（base*2^n + jitter）, max 3 次，支持 `Retry-After` 头
- **代码位置**：`http_client.py` 的 `CS14Client._request()`

#### 5.2 API 语义错误（模型可纠正）
- **症状**：422（无效语言代码）、409（字段锁定）、404（资源不存在）
- **兜底**：handler 返回 `tool_result(is_error=True, message="...")` 而非异常，让模型读错误并调整
- **例子**：`create_survey` 用了 "es"（西班牙语），API 只支持 en/zh/ja/ko，handler 返回"不支持西班牙语"，模型改成 en

#### 5.3 工具参数空间冲突
- **症状**：e.g. likert 题的 `min ≥ max`
- **兜底**：handler 在 `add_*_question` 调用前就做客户端验证，返回 `is_error`，不让坏负载上船
- **代码位置**：`tools/content.py`

#### 5.4 上下文溢出
- **症状**：消息历史堆积导致单次请求超过模型上下文
- **兜底**：`loop.py` 自动修剪旧轮，保留核心状态在 `RunContext`
- **可视化**：JSONL 追踪记录何时做了修剪

#### 5.5 模型服务不可用（RateLimitError, 5xx 耗尽)
- **症状**：`ANTHROPIC_API_KEY` 无效或账户超额
- **兜底**：
  1. SDK 内置重试用尽 → `RealModel.complete()` 捕捉，记录失败原因
  2. 如果配置了 `MODEL_FALLBACK`（e.g. Sonnet 更便宜），自动切换并重试一次（缓存冷了，但跑完）
  3. `--mock` 模式不受影响（从不调 API）
  4. 最坏情况下，`RunResult` 返回 `reason="model_unavailable"`，trace 记录失败点

#### 5.6 工具结果超大
- **症状**：`list_posts` 返回 10MB 的 JSON
- **兜底**：executor 截断至 4000 char，转为摘要 `{count, truncated=true}`
- **权衡**：模型看不到完整列表，但避免了爆炸式的消息体

**设计理念**：
- **临时错误 → 重试**（网络）
- **语义错误 → 报告给模型**（4xx）
- **系统压力 → 降级或降速**（模型 5xx、上下文溢出）
- **永不 silent fail**：所有失败都要么重试、要么进入 JSONL trace、要么返回清晰的 `is_error` 结果

---

## 项目结构

```
cs14/agent/
├── pyproject.toml              # uv 项目；依赖：anthropic, httpx, mcp, pytest, python-dotenv
├── uv.lock
├── README.md                   # 这个文件
├── DESIGN.md                   # 完整技术设计文档
├── .env.example                # 环境变量模板
├── .gitignore
│
├── src/survey_agent/
│   ├── __init__.py
│   ├── config.py               # Settings dataclass：模型 ID、base_urls、凭证、重试参数、截断限制、上下文预算、价格表
│   ├── cli.py                  # `uv run survey-agent` 入口；argparse 参数解析；配置装配
│   ├── loop.py                 # Agent 核心循环（~120 行）；轮次计数、消息历史、停止条件、上下文修剪
│   ├── model.py                # Model 协议；RealModel（包装 SDK）；MockModel（脚本重放）；归一化 ModelResponse
│   │
│   ├── tools/
│   │   ├── __init__.py         # TOOLS：单一有序工具列表（SDK 循环和 MCP 都用）
│   │   ├── schema.py           # ToolSpec dataclass；anthropic_tools() / mcp_tools() 渲染器；parity 测试
│   │   ├── survey.py           # handlers：create_survey, update_survey, get_survey, list_surveys, publish_survey
│   │   ├── content.py          # handlers：add_post, update_post_display, add_comment, add_post_question, add_survey_question
│   │   └── auth.py             # 研究者登录/注册引导（非模型工具）
│   │
│   ├── http_client.py          # CS14Client：JWT bearer, login/register, 重试+退避, 分页工具, 错误映射
│   ├── executor.py             # ToolExecutor：名字→handler 分发、schema 验证、结果截断、异常→is_error 映射
│   ├── trace.py                # Tracer：JSONL 写入器；per-round 摘要；成本计算；RunSummary
│   ├── prompts.py              # SYSTEM_PROMPT（短） + 语言/AB 启发式提示块
│   └── mcp_server.py           # FastMCP 服务器，暴露同样的 TOOLS via stdio；`uv run survey-mcp`
│
├── scripts/
│   ├── demo.sh                 # E2E 演示：启动后端、种子研究者、运行 3 个指令
│   └── run_evals.sh            # 运行评估套件（mock 默认；--real 用真实模型）
│
├── evals/
│   ├── cases/                  # JSON 测试用例（每个 = 指令 + 脚本 + 期望序列 + 终态断言）
│   │   ├── 01_bilingual_ab_xhs.json
│   │   ├── 02_minimal_en_single.json
│   │   └── 03_recover_from_422.json
│   ├── runner.py               # 加载用例、运行循环、应用双层评分、打印记分卡
│   └── graders.py              # sequence_match() + terminal_state_assert()
│
├── tests/
│   ├── test_http_client.py     # 重试/退避、分页形状、错误映射（httpx.MockTransport）
│   ├── test_executor.py        # 截断、参数验证、is_error 映射
│   ├── test_loop_mock.py       # 循环在 MockModel 下驱动到终态，无网络（handlers stubbed）
│   └── test_schema_parity.py   # TOOLS → anthropic_tools() 和 mcp_tools() 同步检查
│
└── traces/                     # 每次运行的 JSONL 追踪输出
    └── <timestamp>-<survey_id>.jsonl
```

---

## 测试

```bash
# 运行全套测试（无网络、无 API key）
uv run pytest

# 运行单个测试文件
uv run pytest tests/test_loop_mock.py -v

# 生成覆盖率报告
uv run pytest --cov=src/survey_agent tests/
```

**测试策略**：
- 所有测试都是离线的（`httpx.MockTransport` for HTTP 层，`MockModel` for 循环）。
- 无后端，无 API key 要求。
- 测试覆盖：重试逻辑、截断、参数验证、mock 循环、schema parity。

---

## 完整示例运行

### 例 1：无 key smoke test
```bash
uv run survey-agent "" --mock --dry-run
# 输出：RunResult with share_code，无网络访问，无 API 调用
```

### 例 2：Mock 脚本，真实后端
```bash
# 需要后端运行（docker compose up -d）
uv run survey-agent "" \
  --mock evals/cases/01_bilingual_ab_xhs.json \
  --trace traces/bilingual_ab.jsonl
# 输出：完整的 JSONL 追踪，显示每一步的决策和 HTTP 状态
```

### 例 3：真实模型（需要 API key）
```bash
export ANTHROPIC_API_KEY=sk-...
uv run survey-agent "做一个Likert量表问卷，中英双语" \
  --model claude-opus-4-8 \
  --trace traces/real_run.jsonl \
  --max-turns 15
# 输出：真实模型驱动的构建，完整追踪和成本统计
```

### 例 4：MCP 服务器
```bash
uv run survey-mcp
# 在另一个终端：Claude Desktop 或其他 MCP 客户端可连接并使用这 13 个工具
```

### 例 5：评估
```bash
# Mock 评估（CI）：快速、确定性
uv run python -m evals.runner

# 真实评估：测量真实模型的通过率和成本
uv run python -m evals.runner --real
```

---

## 未来方向

- **二号工具集** (`--advanced`)：分析、导出、关闭/重开 survey，放在分支后面
- **提示缓存**：工具列表稳定后，冻结系统前缀做 Opus 4.8 缓存
- **公开 URL 与 OG 获取**：目前 demo 默认 mock 以保持网络独立；生产可配置真实 URL 抓取
- **扩展思维** (`extended_thinking`)：等到有真实 key 后验证行为

---

## 快速查阅

| 我想… | 文件 | 关键函数/类 |
|---|---|---|
| 理解架构 | `DESIGN.md` §0-6 | — |
| 改模型逻辑 | `loop.py` | `run(instruction)` |
| 加工具 | `tools/schema.py` | `TOOLS`；`ToolSpec` |
| 加处理器 | `tools/survey.py` 或 `tools/content.py` | `handlers` 字典 |
| 调试工具层 | `http_client.py` | `CS14Client` |
| 写测试 | `tests/test_*.py` | 见模板 |
| 看执行追踪 | `traces/` | JSONL 格式见 `trace.py` |
| 改系统提示 | `prompts.py` | `SYSTEM_PROMPT`, `AB_HEURISTICS` |

---

**维护者**: cs14 Agent 小组  
**状态**: Design v1 · Branch: `feat/survey-builder-agent` · Scope: `cs14/agent/`
