# Phase 4 GitHub App 与 Draft PR 交付说明

## 交付边界

Phase 4 实现受信任 GitHub.com SCM 边界：GitHub App installation 校验、branch 到
不可变 base SHA 的解析、无凭证源码归档、Workspace manifest、Git blob/tree/commit、
确定性 `mewcode/{job_id}` 分支和幂等 Draft Pull Request。生产 Worker 仍不注册真实
Attempt Processor；Phase 5 完成 Verification 后才会把普通 Job 接入发布链路。

Executor 不接收 `.git`、Git remote、App private key 或 installation token。SCM Adapter
只创建新 ref，不更新既有 ref；同名分支必须通过 commit trailer 和 PR marker 证明属于
当前 Job，否则以 `SCM_DELIVERY_CONFLICT` 失败。App JWT、短期 installation token和
平台其他 secrets 会在受信任进程中注册到同一个动态 redactor。

## 配置

Control API 的内置 Resolver 使用：

```text
MEWCODE_PLATFORM_REPOSITORY_RESOLVER_FACTORY=mewcode.platform.scm:create_repository_resolver
MEWCODE_PLATFORM_GITHUB_APP_CLIENT_ID=Iv1...
MEWCODE_PLATFORM_GITHUB_PRIVATE_KEY_FILE=/run/secrets/github_app_private_key
MEWCODE_PLATFORM_GITHUB_TIMEOUT_SECONDS=30
```

App installation 必须具备 Metadata read、Contents write、Pull requests write，且不能有
Workflows write。Compose 从 `.mewcode/secrets/github_app_private_key.pem` 挂载私钥；该
secret 本阶段只提供给 API，不提供给尚未启用的 Worker。

仓库首版不支持 Git LFS、submodule、`.github/**` 修改、GitHub Enterprise Server、
现有分支更新、正式 PR 或合并。默认 Delivery 上限是 200 个变化路径、单文件 5 MiB、
总变化内容 20 MiB。

## 持久化契约

`SUCCEEDED` 必须同时具备正数 `pr_number`、GitHub.com `pr_url`、确定性
`head_branch`、合法 `head_sha` 和成功 Verification。Alembic revision
`0002_phase4_delivery_evidence` 会拒绝包含不完整历史成功记录的升级，不会伪造回填。

## 测试

默认无凭证门禁覆盖 JWT、installation token 权限收窄、归档路径/容量/LFS/submodule
策略、二进制与执行位 diff、Git 对象发布、分支/PR 幂等、冲突恢复、动态 secret
redaction、API 错误和 Delivery 持久化。

真实 GitHub 验收只通过受保护 Environment 手动触发：

```bash
uv run pytest -q --strict-markers --enforce-platform-outcomes \
  -m platform_github_live
```

门禁把固定 SHA 归档导入隔离 Docker Executor，在 Workspace 生成一处受控修改并流式
导出，然后在专用测试仓库创建 Draft PR。它重复发布并验证同一分支、head SHA、PR
number以及 open draft 状态，随后在 `finally` 中关闭 PR并删除测试分支。默认 CI
明确排除该 marker。
