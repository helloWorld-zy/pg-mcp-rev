# pg-mcp 已知问题和设计实现差距

本文档记录了设计规范与实际实现之间的差距，以及需要关注的代码缺陷。

---

## 1. 多数据库与安全控制功能未完全实现

### 问题描述
多数据库与安全控制功能虽在设计中有承诺，但实际未能启用：服务器始终使用单一执行器，无法强制实施表/列访问限制或 EXPLAIN 策略，这可能导致请求访问错误数据库，且敏感对象无法得到保护。

### 具体问题

#### 1.1 单一执行器问题

**设计承诺**（参见 `specs/0002-pg-mcp-design.md`）：
- 支持多数据库配置 `databases: list[DatabaseConfig]`
- 每个数据库独立的执行器和连接池

**实际实现**（`src/pg_mcp/server.py` 第 197 行）：
```python
# 创建了多个执行器...
sql_executors: dict[str, SQLExecutor] = {}
for db_name, pool in _pools.items():
    executor = SQLExecutor(...)
    sql_executors[db_name] = executor

# ...但只传入了主数据库的执行器到 orchestrator
_orchestrator = QueryOrchestrator(
    ...
    sql_executor=sql_executors[_settings.database.name],  # 仅使用主执行器
    ...
)
```

**影响**：
- 即使配置了多个数据库，查询也只会发送到主数据库
- 用户指定非默认数据库时可能导致混淆或错误

#### 1.2 表/列访问限制未传递

**设计承诺**（参见 `specs/0002-pg-mcp-design.md` 第 228-229 行）：
```python
class SecurityConfig(BaseSettings):
    blocked_tables: list[str] = Field(default_factory=list)
    blocked_columns: list[str] = Field(default_factory=list)
```

**实际实现**：

1. `config/settings.py` 中 `SecurityConfig` 类**缺少** `blocked_tables` 和 `blocked_columns` 字段：
```python
class SecurityConfig(BaseSettings):
    allow_write_operations: bool = Field(...)
    blocked_functions: list[str] = Field(...)
    max_rows: int = Field(...)
    max_execution_time: float = Field(...)
    readonly_role: str | None = Field(...)
    safe_search_path: str = Field(...)
    # 缺少 blocked_tables 和 blocked_columns
```

2. `server.py` 第 153-158 行创建 SQLValidator 时显式传入 `None`：
```python
sql_validator = SQLValidator(
    config=_settings.security,
    blocked_tables=None,  # 硬编码为 None
    blocked_columns=None,  # 硬编码为 None
    allow_explain=False,
)
```

**影响**：
- 无法配置敏感表/列访问限制
- 潜在的数据泄露风险

#### 1.3 EXPLAIN 策略硬编码禁用

**设计承诺**：`allow_explain` 应可通过 `SecurityConfig` 配置

**实际实现**（`server.py` 第 157 行）：
```python
allow_explain=False,  # 硬编码禁用
```

**影响**：无法通过配置启用 EXPLAIN 功能

---

## 2. 弹性与可观测性模块未整合到请求处理流程

### 问题描述
弹性与可观测性模块（如速率限制、重试/退避机制、指标/追踪系统）仅停留在设计层面，尚未整合到实际请求处理流程中。

### 具体问题

#### 2.1 速率限制器创建但未使用

**设计承诺**（参见 `specs/0002-pg-mcp-design.md`）：
- 速率限制器应限制并发查询和 LLM 调用

**实际实现**（`server.py` 第 186-190 行）：
```python
# 创建了速率限制器
_rate_limiter = MultiRateLimiter(
    query_limit=10,
    llm_limit=5,
)
```

然而，在 `query()` 函数中（第 252-373 行）完全没有使用 `_rate_limiter`：
```python
@mcp.tool()
async def query(...) -> dict[str, Any]:
    # 没有调用 _rate_limiter.for_queries() 或 _rate_limiter.for_llm()
    response = await _orchestrator.execute_query(request)
    ...
```

**影响**：
- 系统无法限制并发请求数量
- 可能导致资源耗尽
- 与设计文档承诺的 "最大并发查询" 限制不符

#### 2.2 熔断器未在 orchestrator 外部使用

虽然 `QueryOrchestrator` 内部使用了熔断器（用于 LLM 调用），但全局的 `_circuit_breaker` 变量创建后未被使用。

#### 2.3 重试/退避机制未完整实现

**设计承诺**（参见 `specs/0001-pg-mcp-prd.md`）：
- 数据库瞬时错误指数退避重试，最多 3 次

**实际实现**（`config/settings.py`）：
```python
class ResilienceConfig(BaseSettings):
    max_retries: int = Field(default=3)
    retry_delay: float = Field(default=1.0)
    backoff_factor: float = Field(default=2.0)
```

配置存在，但 `SQLExecutor.execute()` 方法中没有实现重试逻辑。

#### 2.4 追踪系统未集成

**设计承诺**：
- 支持 OpenTelemetry 标准
- 关键 span：`sql_generation`、`sql_validation`、`sql_execution`、`result_validation`

**实际实现**：
- `observability/tracing.py` 存在但未在任何服务中使用
- 没有 span 包装任何操作

#### 2.5 Metrics 未完全集成

**设计承诺**（参见 `specs/0001-pg-mcp-prd.md`）：
```
| 指标名称                      | 说明                           |
|-------------------------------|--------------------------------|
| `query_requests_total`        | 查询请求总数（按状态分类）     |
| `query_duration_seconds`      | 查询端到端耗时分布             |
| `llm_calls_total`             | LLM 调用次数（生成/验证）      |
...
```

**实际实现**：
- `MetricsCollector` 类存在但 metrics 未在请求流程中记录
- 仅启动了 Prometheus HTTP 服务器，但未收集实际指标

---

## 3. 响应/模型缺陷及测试覆盖不足

### 问题描述
响应/模型缺陷（重复的 to_dict 方法、未使用的配置字段）及测试覆盖不足，导致当前系统行为偏离实施方案，且难以进行有效验证。

### 具体问题

#### 3.1 `QueryResponse.to_dict()` 方法重复定义

**位置**：`src/pg_mcp/models/query.py`

同一个类中定义了两个 `to_dict()` 方法：

```python
class QueryResponse(BaseModel):
    ...

    def to_dict(self) -> dict[str, Any]:  # 第一次定义（第 160-173 行）
        """Convert response to dictionary for MCP tool return."""
        result = self.model_dump(exclude_none=False)
        if result.get("tokens_used") is None:
            result["tokens_used"] = 0
        return result

    ...

    def to_dict(self) -> dict[str, Any]:  # 第二次定义（第 214-220 行）
        """Convert response to dictionary."""
        return self.model_dump(exclude_none=True)
```

**影响**：
- 第二个定义会覆盖第一个
- 行为与第一个定义的文档描述不一致
- `tokens_used` 的默认值处理逻辑丢失
- 容易引起混淆和 bug

#### 3.2 未使用的配置字段

设计文档中存在但未在实际配置中实现的字段：

| 设计中的字段 | 状态 |
|--------------|------|
| `SecurityConfig.blocked_tables` | 未实现 |
| `SecurityConfig.blocked_columns` | 未实现 |
| `DatabaseConfig.ssl_mode` | 未实现 |
| `ValidationConfig.max_retries` (结果验证重试) | 未实现 |

#### 3.3 测试覆盖不足

**现有测试**：
- `tests/unit/` - 单元测试覆盖各组件
- `tests/integration/test_full_flow.py` - 集成测试

**缺失的测试场景**：

1. **多数据库切换测试**：没有测试多个数据库配置时的正确数据库选择
2. **速率限制集成测试**：测试文件 (`test_resilience.py`) 仅测试组件本身，未测试与请求流程的集成
3. **敏感表/列拦截测试**：由于功能未实现，相关测试也不存在
4. **追踪/指标验证测试**：没有验证 OpenTelemetry span 或 Prometheus 指标是否正确发出

---

## 建议修复优先级

| 优先级 | 问题 | 原因 |
|--------|------|------|
| P0 (紧急) | 多数据库执行器选择 | 功能性错误，可能导致查询发送到错误的数据库 |
| P0 (紧急) | 速率限制器集成 | 安全风险，系统可能被滥用导致资源耗尽 |
| P1 (高) | 添加 blocked_tables/columns 配置 | 安全功能缺失 |
| P1 (高) | 修复 to_dict 重复定义 | 代码缺陷，行为不可预测 |
| P2 (中) | 追踪/指标集成 | 可观测性缺失，影响生产环境问题排查 |
| P2 (中) | 添加集成测试 | 提高代码质量和维护性 |

---

## 文件清单

以下是需要修改的主要文件：

- `src/pg_mcp/server.py` - 修复多数据库执行器选择和速率限制器集成
- `src/pg_mcp/config/settings.py` - 添加缺失的配置字段
- `src/pg_mcp/models/query.py` - 修复重复的 to_dict 方法
- `src/pg_mcp/services/orchestrator.py` - 添加追踪 span 包装
- `src/pg_mcp/services/sql_executor.py` - 添加重试逻辑
- `tests/integration/` - 添加缺失的集成测试
