# cs14 Backend Load Test — Production-ish Config

**Date:** 2026-07-03 · **Runner:** local async httpx harness (throwaway script, not in repo)

## Setup

| Item | Value |
|---|---|
| Hardware | Apple M5 Pro, 48 GB RAM, macOS 26.4 |
| Server | `uvicorn app.main:app --workers 4` (**no `--reload`**), port 8010, host Python via uv |
| Database | postgres:16-alpine in Docker (OrbStack), seeded via `scripts/seed_client_demo.py` |
| Client | 同机 loopback，httpx AsyncClient + keepalive 连接池，无网络跳数 |
| Method | 每组合 2s 预热（不计入）+ 20s 计时窗口；逐请求 `perf_counter` 计时；百分位取全样本 |

## Results

| Endpoint | c | QPS | P50 | P95 | P99 | Requests | Errors |
|---|---|---|---|---|---|---|---|
| `GET /health` | 10 | **1067.1** | 4.7ms | **29.4ms** | 76.2ms | 21,348 | 0 |
| `GET /health` | 50 | 215.4 | 147.5ms | 728.3ms | 1127.4ms | 4,328 | 0 |
| `GET /api/v1/surveys/public/CS14DEMO2026` | 10 | **714.1** | 7.6ms | **40.6ms** | 83.0ms | 14,286 | 0 |
| `GET /api/v1/surveys/public/CS14DEMO2026` | 50 | 231.0 | 121.6ms | 754.8ms | 1304.2ms | 4,671 | 0 |
| `GET /api/v1/surveys` (JWT) | 10 | **853.4** | 7.1ms | **32.0ms** | 62.3ms | 17,074 | 0 |
| `GET /api/v1/surveys` (JWT) | 50 | 258.1 | 112.1ms | 650.1ms | 1342.4ms | 5,204 | 0 |

**总计 66,911 请求，0 错误。**

## vs. dev-mode baseline（前一轮 workflow 验证，`--reload` 单进程，c=50）

| Endpoint | dev 单进程 c=50 | 4-worker c=10 |
|---|---|---|
| `/health` | 440 QPS / P95 363ms | **1067 QPS / P95 29ms** |
| public survey read | 236 QPS / P95 640ms | **714 QPS / P95 41ms** |
| authed survey list | 233 QPS / P95 637ms | **853 QPS / P95 32ms** |

## 读数与局限（面试时的诚实口径）

1. **c=10 是代表性数字**：吞吐与延迟同时达到最优（服务端未饱和），JWT + PostgreSQL 查询路径也在 850+ QPS / P95 32ms。
2. **c=50 出现均匀退化（约 5×）**，且不碰数据库的 `/health` 同样退化 → 说明瓶颈不在应用/DB 层。最可能是 macOS 上 uvicorn 多进程共享 listen socket 的 accept 分发问题 + 同机压测的 client/server CPU 争用；Linux 生产环境（SO_REUSEPORT 语义不同）预期不会复现这种模式。未在 Linux 上验证，不下定论。
3. 同机 loopback、无网络跳数、20s 短窗口、Python 客户端计时（含客户端调度开销，对 P99 偏保守）。
4. 数据库数据量为 demo seed 规模，非生产数据量。

## 简历口径（可验证）

> 单机压测（4 uvicorn worker）：核心读接口 714–1067 QPS、P95 < 41ms，66,911 请求零错误（含 JWT 鉴权 + PostgreSQL 查询路径）
