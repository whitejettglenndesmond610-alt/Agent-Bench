# 从零部署指南（第三方快速上手）

从一台干净的机器到平台跑起来，按顺序执行即可。两条路径：**Docker 一体化**（推荐，最省心）和**本地开发模式**。

## 0. 环境要求

| 方式 | 需要 |
| --- | --- |
| Docker 一体化 | Docker（或 Podman）+ Docker Compose |
| 本地开发 | Python 3.12、`uv`、Node 24、pnpm 11 |

```bash
git clone <repo-url> && cd xiangmu
```

## 1. 生成密钥（两种方式都需要）

平台有三类密钥，全部自己生成，仓库不含任何默认值：

```bash
# 登录密码哈希（两个角色：只读 / 操作员，各生成一次，妥善保存输入的明文密码）
uv run python -m agent_eval.maintenance.main hash-password

# 数据加密密钥（加密存储 Agent token；丢失后已存的 token 无法解密）
uv run python -m agent_eval.maintenance.main generate-encryption-key
```

复制 `.env.example` 为 `.env`，把生成的值填进去：

- `AGENT_EVAL_READ_ONLY_PASSWORD_HASH` / `AGENT_EVAL_OPERATOR_PASSWORD_HASH`（Argon2 哈希含 `$`，写进 `.env` 时保持单行即可）
- `AGENT_EVAL_ENCRYPTION_KEY`

## 2A. Docker 一体化部署（推荐）

```bash
cd deploy
# 创建 .env（变量名见 .env.example / compose 文件提示），至少需要：
#   POSTGRES_PASSWORD=随机字符串
#   AGENT_EVAL_MINIO_ACCESS_KEY=随机用户名
#   AGENT_EVAL_MINIO_SECRET_KEY=随机密码
#   AGENT_EVAL_BACKUP_PATH=/abs/path/to/backups
#   AGENT_EVAL_LOCAL_READ_ONLY_PASSWORD_HASH=<第 1 步生成>
#   AGENT_EVAL_LOCAL_OPERATOR_PASSWORD_HASH=<第 1 步生成>
#   AGENT_EVAL_LOCAL_ENCRYPTION_KEY=<第 1 步生成>
docker compose -f compose.yaml -f compose.local-real.yaml -f compose.postgres.yaml -f compose.minio.yaml up -d --build
```

平台在 `http://localhost:8080`（Web + API 同端口，Nginx 反代）。数据库迁移在启动时自动执行。

## 2B. 本地开发模式（SQLite，零容器）

```bash
uv sync && pnpm install
uv run alembic upgrade head          # 在 ./data/agent-eval.db 建库
uv run uvicorn agent_eval.api.main:app --reload   # 终端 1：API  http://localhost:8000
uv run python -m agent_eval.worker.main           # 终端 2：Worker
pnpm dev:web                                       # 终端 3：Web  http://localhost:5173
```

开发模式下密码哈希/加密密钥不配也能启动（仅生产强制），但登录功能需要第 1 步的配置。

## 3. 让它"有题可考"：导入演示题库

不导入题库时平台只有 smoke 档。最快见效的是内置演示集（150 题，离线可复现构建）：

```bash
uv run python -m agent_eval.maintenance.main build-demo-dataset
uv run python -m agent_eval.maintenance.main import-dataset data/demo-dataset --require-demo-distribution
```

## 4. 没有真 Key 也能体验全链路（演示模式）

平台自带假 Agent / 假裁判服务，零成本体验"创建评测 → 执行 → 打分 → 出榜"：

```bash
uv run python -m agent_eval.maintenance.main seed-fake-agents
uv run python -m agent_eval.maintenance.main queue-fake-run --questions 20
uv run python -m agent_eval.maintenance.main wait-run
```

## 5. 接入真实能力（可选，按需配置）

| 能力 | 需要什么 | 配置方式 |
| --- | --- | --- |
| 开放题裁判打分 / 失败聚类 / τ-bench 用户模拟 | 一个 DeepSeek API Key | `AGENT_EVAL_DEEPSEEK_API_KEY`（Docker 场景是 `AGENT_EVAL_LOCAL_DEEPSEEK_API_KEY`） |
| 被测的第三方 Agent | 各家平台自己的 API Key | 网页端「Agent 中心」或「我的 API」里注册，Key 加密落库 |
| 防污染题库 heldout-v1（100 题） | LiveBench 公开可拉；HLE 需在 HuggingFace 接受条款后用自己 token 下载 | 见 README「数据」一节 |
| τ-bench 交互任务（278 题） | clone 公开仓库 + DeepSeek Key | 见 README「数据」一节 |
| MCPMark 文件操作任务（40 题） | clone 公开仓库；Worker 镜像已内置 Node | 见 README「数据」一节 |
| Langfuse 调用追踪 | 自托管 Langfuse 实例 + 项目 API Key | `LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY`，不配则完全关闭 |

## 6. 验证安装

```bash
uv run ruff check . && uv run mypy && uv run pytest     # 后端
pnpm lint:web && pnpm test:web && pnpm build:web        # 前端
pnpm test:e2e                                           # 端到端（需先 build）
```

## 常见问题

- **打开页面要求登录**：用第 1 步设置密码哈希时输入的明文密码，选「角色密码」登录（read_only 或 operator）
- **评测跑完开放题没分数**：没配 DeepSeek Key，裁判不可用（选择题/交互任务不受影响）
- **榜单是空的**：榜单只收录 `ranking_eligible` 的正式档（unified-v3/official-v4/heldout-v1）；smoke、tau2、MCPMark 属实验档不进总榜
- **生产部署**：不要直接暴露上面的 compose；用 `deploy/manage.sh` + `.env.production`，详见 [`deploy/runbook.md`](runbook.md)

> 不要提交 `.env`、真实数据集、数据库、Artifact、备份、登录凭据、Agent Token 或第三方 API Key。
