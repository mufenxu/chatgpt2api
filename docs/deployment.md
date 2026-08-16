# 部署与升级指南

本文介绍 ChatGPT2API 的常见部署方式，以及后续升级项目时需要保留的数据和执行步骤。

## 部署前准备

直接拉取镜像部署时，服务器只需要安装 Docker：

- Docker

WARP / FlareSolverr 部署额外需要 Docker Compose v2；源码运行才需要 Git。首次部署前建议确认：

```bash
docker version
```

项目核心持久化文件：

| 路径 | 作用 |
| --- | --- |
| `config.json` | 主配置、后台密钥、代理、图片、备份等配置 |
| `.env` | Docker compose 环境变量 |
| `data/` | 账号、日志、图片、任务记录等运行数据 |

升级和迁移时重点保留以上内容。

## 方式一：普通 Docker 部署

适合不需要 WARP / FlareSolverr 清障的场景。正式部署不需要克隆仓库，直接使用 GitHub Container Registry 的预构建镜像：

```text
ghcr.io/mufenxu/chatgpt2api:latest
```

镜像支持 `linux/amd64` 和 `linux/arm64`。仓库每次推送代码后，GitHub Actions 会自动构建并更新 `latest` 标签。

如果 GHCR 包设置为私有，首次拉取前登录：

```bash
docker login ghcr.io -u mufenxu
```

登录时使用具有 `read:packages` 权限的 GitHub Personal Access Token；公开 GHCR 包不需要登录。

创建持久化目录和配置文件：

```bash
mkdir -p /opt/chatgpt2api/data
cat > /opt/chatgpt2api/config.json <<'JSON'
{
  "auth-key": "replace-with-your-admin-key"
}
JSON
cat > /opt/chatgpt2api/.env <<'ENV'
MY_ISSUER=https://your-oidc-provider.example.com
MY_CLIENT_ID=replace-with-your-client-id
MY_CLIENT_SECRET=replace-with-your-client-secret
MY_REDIRECT_URI=https://your-domain.com/auth/my/callback
SESSION_SECRET=replace-with-at-least-32-random-characters
ENV
```

首次启动：

```bash
docker pull ghcr.io/mufenxu/chatgpt2api:latest
docker run -d \
  --name chatgpt2api \
  --restart unless-stopped \
  -p 6066:80 \
  --env-file /opt/chatgpt2api/.env \
  -v /opt/chatgpt2api/data:/app/data \
  -v /opt/chatgpt2api/config.json:/app/config.json \
  -e STORAGE_BACKEND=json \
  ghcr.io/mufenxu/chatgpt2api:latest
```

修改 `config.json` 中的 `auth-key` 后再启动。

访问：

```text
http://localhost:6066
```

API 基础地址：

```text
http://localhost:6066/v1
```

查看日志：

```bash
docker logs -f chatgpt2api
```

停止：

```bash
docker rm -f chatgpt2api
```

更新镜像：

```bash
docker pull ghcr.io/mufenxu/chatgpt2api:latest
docker rm -f chatgpt2api
# 再次执行上面的 docker run 命令
```

默认部署启用统一登录，因此必须通过 `--env-file /opt/chatgpt2api/.env` 传入五个 OIDC 参数。`MY_REDIRECT_URI` 必须与统一登录服务中登记的回调地址完全一致。如果 GHCR 包设置为私有，先执行 `docker login ghcr.io -u mufenxu`。

### GitHub Actions 镜像构建

镜像构建工作流位于 `.github/workflows/docker-publish.yml`，会在代码每次推送到 GitHub 时运行，也可以在 GitHub Actions 页面手动运行。所有构建结果统一推送到：

```text
ghcr.io/mufenxu/chatgpt2api:latest
```

工作流使用 Docker Buildx 构建 `linux/amd64` 和 `linux/arm64` 两个平台。服务器执行 `docker pull ghcr.io/mufenxu/chatgpt2api:latest` 后即可获取最近一次推送对应的镜像。

## 方式二：WARP / FlareSolverr 部署

适合上游请求经常遇到 Cloudflare 拦截的场景。该方式会启动：

- `warp-proxy`
- `privoxy`
- `flaresolverr`
- `init-config`
- `app`

复制环境变量模板：

```bash
cp config.example.json config.json
cp .env.example .env
```

`config.json` 和 `.env` 都是必需的；`.env` 中除了统一登录参数，也可以按需修改端口、代理和 FlareSolverr 配置。

至少修改 `.env` 中的统一登录参数：

```text
MY_ISSUER=https://your-oidc-provider.example.com
MY_CLIENT_ID=replace-with-your-client-id
MY_CLIENT_SECRET=replace-with-your-client-secret
MY_REDIRECT_URI=https://your-domain.com/auth/my/callback
SESSION_SECRET=replace-with-at-least-32-random-characters
```

启动：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

访问：

```text
http://localhost:6066
```

FlareSolverr 相关配置可以在后台设置页的 `FlareSolverr` tab 中查看和测试。

查看容器状态：

```bash
docker compose -f docker-compose.warp.yml ps
```

查看日志：

```bash
docker logs -f chatgpt2api-warp
docker logs -f chatgpt2api-flaresolverr
```

停止：

```bash
docker compose -f docker-compose.warp.yml down
```

## 方式三：源码运行

适合本地开发或临时调试。

后端：

```bash
git clone git@github.com:mufenxu/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

前端开发服务：

```bash
cd web
bun install
bun run dev
```

源码方式运行时，后端默认读取项目根目录的 `config.json` 和 `data/`。

## 存储后端

默认使用本地 JSON 文件：

```text
STORAGE_BACKEND=json
```

可选值：

| 值 | 说明 |
| --- | --- |
| `json` | 本地 JSON 文件，默认方式 |
| `sqlite` | 本地 SQLite，通常存放在 `data/accounts.db` |
| `postgres` | 外部 PostgreSQL |
| `git` | Git 私有仓库存储账号数据 |

PostgreSQL 示例：

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

SQLite 示例：

```yaml
environment:
  - STORAGE_BACKEND=sqlite
  - DATABASE_URL=sqlite:////app/data/accounts.db
```

## 升级前备份

升级前建议备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

如果没有 `.env`，可以去掉：

```bash
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json data
```

也可以在后台设置页配置 Cloudflare R2 备份，用于定时备份关键数据。

## 升级：普通 Docker 部署

进入项目目录：

```bash
cd chatgpt2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码和镜像：

```bash
git pull
docker compose pull
docker compose up -d
```

查看状态：

```bash
docker compose ps
docker logs -f chatgpt2api
```

## 升级：WARP / FlareSolverr 部署

进入项目目录：

```bash
cd chatgpt2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码并重新构建：

```bash
git pull
docker compose -f docker-compose.warp.yml up -d --build
```

查看状态：

```bash
docker compose -f docker-compose.warp.yml ps
docker logs -f chatgpt2api-warp
```

## 升级：源码运行

```bash
cd chatgpt2api
git pull
uv sync
```

如果需要重新构建前端静态产物：

```bash
cd web
bun install
bun run build
```

然后按你的进程管理方式重启后端服务。

## 回滚

如果升级后需要回滚代码：

```bash
git log --oneline -n 20
git checkout <旧版本commit>
```

普通 Docker 部署：

```bash
docker compose up -d
```

WARP / FlareSolverr 部署：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

如果需要恢复数据：

```bash
tar -xzf backups/你的备份文件.tgz
```

恢复数据前建议先停止容器，避免运行中写入覆盖：

```bash
docker compose down
```

或：

```bash
docker compose -f docker-compose.warp.yml down
```

## 常用维护命令

查看容器：

```bash
docker compose ps
```

查看主服务日志：

```bash
docker logs -f chatgpt2api
```

查看 WARP 部署主服务日志：

```bash
docker logs -f chatgpt2api-warp
```

重启普通部署：

```bash
docker compose restart
```

重启 WARP 部署：

```bash
docker compose -f docker-compose.warp.yml restart
```

清理未使用镜像：

```bash
docker image prune
```

不要直接删除 `data/`、`config.json`、`.env`，除非已经确认有可用备份。
