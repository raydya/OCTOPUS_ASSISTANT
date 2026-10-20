# Octopus Assistant

Python 3.12 + FastAPI + PostgreSQL + Docker 的个人飞书工作台。提供对话和审批读取、持续审批检查，以及按已核验本地规则自动同意审批。自动回复尚未实现。

使用 **WebSocket 长连接接收事件 + OAuth 设备授权**，无需配置 HTTP 回调地址、公网域名、内网穿透或飞书授权重定向 URL。

## 启动

需要 Docker Engine / Docker Desktop、Docker Compose，以及用于生成配置的 Python 3。

```bash
python3 scripts/setup_env.py
# Configure DATABASE_URL and MIGRATION_DATABASE_URL, then start local-infra first.
docker compose --profile maintenance run --rm --build migrate
docker compose up -d --build app worker
```

首次运行启动命令前，在 `.env` 填写 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 和共享数据库连接。配置脚本不会覆盖已有 `.env`；随机密钥仅写入文件，权限为 `0600`。

访问 `APP_BASE_URL`，默认 [http://localhost:8000](http://localhost:8000)，使用 `.env` 的 `APP_PASSWORD` 登录。点击“连接飞书”，再点击“打开飞书授权页面”，确认授权后回到工作台。页面会自动检测授权完成并开始同步。

本机端口被占用时，同时修改 `APP_PORT` 和 `APP_BASE_URL`，例如 `8180` 和 `http://localhost:8180`。**这个地址用于访问工作台，不需要填进飞书回调配置。**

本项目运行两个常驻容器，PostgreSQL 由独立的 `local-infra` Compose 管理：

| 服务 | 职责 |
| --- | --- |
| `app` | 工作台、设备授权、只读数据接口；使用受限运行账号 |
| `worker` | 主动连接飞书 WSS、接收事件、定时读取个人数据、检查待审批并执行匹配规则；使用同一运行账号 |
| `migrate` | maintenance profile 下的一次性 Alembic 迁移；仅使用本应用 owner 账号 |

共享 PostgreSQL 使用外部内部网络 `local-infra-assistant-db`，服务地址为 `shared-postgres:5432`。本应用独占数据库 `octopus_assistant`，不与 Codex bot 共用数据库或账号。`local-infra` 必须先创建该网络、数据库和权限。

- `DATABASE_URL`：`octopus_assistant_runtime` 身份，仅有本应用所需读写权限。
- `MIGRATION_DATABASE_URL`：`octopus_assistant_owner` 身份，用于表结构迁移；该变量不会传入 `app` 或 `worker`。
- `DATABASE_WAIT_SECONDS`：启动时等待数据库与当前 Alembic 版本的期限，默认 60 秒；单次连接最多 5 秒、探测语句最多 3 秒，最后一次探测可能略超出期限。超时退出后由 Docker 重启策略重试。`/healthz` 同时检查连接和迁移版本。

每次更新先运行迁移服务，再启动应用；运行进程不自动执行 DDL。既有库恢复后由基础设施流程修正 owner、运行账号授权及未来对象的默认权限；`alembic_version` 只授予运行账号 SELECT。不要将管理员连接写入 `DATABASE_URL`。

`worker` 没有入站端口；Compose 仅将工作台端口绑定到本机。`app` 和 `worker` 另接本项目的出站网络访问飞书 HTTPS / WSS。停止本项目不会停止共享 PG；不要删除基础设施数据卷。

```bash
docker compose ps
docker compose logs --tail 50 app worker
# 修改环境变量后让两个进程重新加载配置：
docker compose up -d --force-recreate app worker
# 保留数据卷，停止服务：
docker compose stop
```

## 飞书后台配置

在已有企业自建应用中：

1. 开通下面的用户身份权限，并将本人加入应用可用范围。
2. 在“事件与回调 → 事件配置”选择**使用长连接接收事件**。应用启动后可建立连接，无需填写请求地址。
3. 添加后台可选的所需事件并发布版本。仅在本地注册事件处理器不会替代飞书后台的事件订阅；实例状态事件不是应用运行的前提。
4. 回到工作台完成设备授权。App ID / Secret 只用于应用身份，个人数据仍需本人授权。

| 事件 | 用途 |
| --- | --- |
| `im.message.receive_v1` | 机器人可见消息的更新提示；要求机器人能力及相应应用消息读取权限 |
| `approval.instance.status_changed_v4` | 可选增强：个人相关审批实例变化；后台未提供该选项时可跳过 |
| `approval.task.status_changed_v4` | 个人相关审批任务变化 |

后台进程会用用户令牌调用审批订阅接口，注册 `INVOLVED_APPROVAL`（本人发起或参与的审批）。这与开放平台的事件订阅共同生效；停掉进程不会取消飞书端的审批订阅关系。

`subscriptions=subscribed` 仅表示用户身份的审批订阅 API 注册成功，不代表后台事件配置已生效，也不代表实例事件已经实际送达。实例事件在当前应用后台不可选时，保留任务事件，并由任务变化触发详情读取、定时同步补齐状态；是否已接通某种实时事件，应以实际收到该类型的事件为准。

| 用户身份权限 | 用途 |
| --- | --- |
| `offline_access` | 令牌自动刷新 |
| `contact:user.base:readonly` | 确认当前授权账号 |
| `im:chat:read` | 个人会话列表 |
| `im:message:readonly` | 历史消息 |
| `im:message.p2p_msg:get_as_user` | 个人单聊消息 |
| `im:message.group_msg:get_as_user` | 个人群聊消息 |
| `approval:task:read` | 待办、已办和知会列表；任务变化订阅 |
| `approval:instance:read` | 审批详情；实例变化订阅 |
| `approval:task:write` | 按已核验规则同意本人待办；没有此授权时仍可读取和检查 |

工作台按 `.env` 的 `FEISHU_SCOPES` 请求授权。更新权限需要发布应用并重新授权。单聊/群聊及审批的实际可见范围仍由飞书和组织权限决定。首次授权后会固定绑定账号和租户；可提前用 `FEISHU_ALLOWED_OPEN_ID` 限制账号。

## 持续检查与自动审批

`APPROVAL_AUTOMATION_ENABLED=true` 时，worker 独立检查待审批。相同任务两次检查至少间隔 60 秒，不受历史消息队列阻塞。工作台的审批列表、详情和连接页会展示判断原因、规则版本、提交与确认结果。

当前使用确定性的本地规则，不调用语言模型。`approval-policy.json` 只读挂载到容器，保存经过业务核验的通过条件；它可能含内部业务内容，已排除 Git 和镜像构建上下文。首次运行 `scripts/setup_env.py` 会从 `approval-policy.example.json` 创建空政策，不覆盖既有文件。规则为空时只检查并列出介入原因，不会自动同意。

规则完整匹配审批定义、表单字段 ID、名称、类型和允许值；未知定义、新增字段、不可解析表单或多个规则同时匹配，均保留待办。表单说明和评论只作为数据，不能修改政策或授予操作权限。具体单据授权使用 `task_ids` 和 `instance_codes` 同时限定；不要将某一单据的确认扩展为整个审批类别的无限授权。

政策格式（示例标识不对应真实审批，按实际核验结果配置）：

```json
{
  "version": "policy-v1",
  "rules": [{
    "id": "verified-task",
    "definition_code": "DEFINITION_CODE",
    "definition_name": "已核验的审批定义",
    "task_ids": ["TASK_ID"],
    "instance_codes": ["INSTANCE_CODE"],
    "rationale": "记录业务核验依据与授权范围",
    "fields": {
      "FIELD_ID": {"name": "环境", "type": "input", "values": ["隔离测试"]}
    }
  }]
}
```

`fields` 必须覆盖全部非说明控件；复选框值若为数组，允许值写为 `[["选项一", "选项二"]]`，不会把数组拆成任意组合。表单精确快照还可以通过 `form_sha256` 限定。政策每轮重读；更改后无需重建镜像，使用替换文件的编辑器时需重建容器以刷新绑定挂载。

实际提交前重新读取飞书待办和详情，确认任务属于绑定用户、节点仍待处理、支持 API 操作且权限齐全。当前只检查最新待办第一页能明确找到的任务；超过第一页的任务保留人工处理，不凭旧缓存提交。只支持“同意”，不自动拒绝或修改审批表单。

提交标记先写入 PostgreSQL，再发送一次 `POST /approval/v4/tasks/pass`。请求超时、错误或进程重启后，只回读状态，不自动重试提交。只有同一实例、同一任务、同一用户的任务状态返回 `APPROVED`，才显示“已确认通过”；实例完成或待办消失都不作为通过证据。结果不确定会持续显示“待核实”。

设置 `APPROVAL_AUTOMATION_ENABLED=false` 并重建容器可暂停检查和后续提交；已经发出的请求可能仍会完成。首次增加写权限后需在工作台重新授权。

## 长连接和个人数据范围

飞书 `im.message.receive_v1` 是**机器人身份事件**，不会推送个人账号的所有聊天。因此应用同时采用：

- **长连接事件**：已配置且可投递的审批状态事件，以及机器人可见的消息变化。先将事件 ID 和资源标识写入 PostgreSQL，提交成功后才应答；重复推送幂等处理。
- **个人数据补齐**：默认每 60 秒安排会话、近 7 天消息、已知话题回复及四类审批列表的同步，使用用户令牌读取。`SYNC_INTERVAL_SECONDS` 可调整周期；实际完成时间受数据量、分页及飞书限流影响。
- **手动历史读取**：可选近 7 / 30 / 90 / 365 天；后台继续已保存的分页进度。话题回复独立分页。

事件只作为更新提示。消息正文及审批详情通过**绑定账号的用户令牌**读取，机器人所在但本人不可见的聊天内容不会被直接导入个人工作台。附件目前展示类型和原始结构，不下载二进制内容。“我发起的审批”单独列表尚未提供。

SDK 处理 WSS 协议、分片与心跳；进程监督重连并限制获取连接地址的 HTTP 超时。数据库锁确保同一实例仅一个事件接收进程；启动连接前检查飞书远端在线数，检测到其他连接时显示“该应用已有其他长连接”，避免多接收器分流事件。

长连接断开期间不承诺飞书补发全部事件，定时同步用于收敛缓存。超过默认 7 天窗口的消息编辑或撤回，需要手动扩大同步范围。此版本不提供无限历史的连续增量游标。

## 同步、授权和数据保存

- 页面读取本地缓存，并每 10 秒刷新状态。所有业务读取均使用 `user_access_token`；应用令牌只用于长连接连接数检查。
- `SYNC_PAGE_LIMIT` 默认 5，控制每批最大页数。数据和分页游标在一个事务提交；失败回滚当前批，已完成批次保留。后台自动续传，有需要也可手动继续或重新开始。
- 消息按 `message_id` 幂等更新；审批按 `(task_id, topic)` 保存分类。完整同步结束后才隐藏未出现的旧列表成员。待办消失不会被推断为审批通过。
- 同步任务持久化，可在进程重启后继续；临时错误退避重试。事件去重记录保留 7 天，仅记录事件 ID 和资源标识，不保存事件消息正文。
- 会话与审批列表优先完成分页，随后优先处理近 30 分钟消息事件对应的会话和待审批详情，避免新数据排在大量历史任务之后。事件中的未知会话最多额外完整补查一次；失败任务仍遵守退避时间。
- 设备授权绑定工作台浏览器会话，遵守飞书轮询间隔、`slow_down`、拒绝和过期状态。设备码和换得的令牌加密保存，浏览器只收到授权链接及用户确认码。
- 用户访问/刷新令牌使用 Fernet 加密，刷新用行锁串行化。消息正文和审批内容存于普通数据库字段及 JSONB，数据库本身并非全库加密。请保留 `TOKEN_ENCRYPTION_KEY`。
- 退出工作台结束浏览器会话和未完成的设备授权，后台同步继续。停止后台同步可执行 `docker compose stop worker`；撤销用户授权需在飞书账号授权管理中操作。

## 接口

除了首页、静态文件、登录和 `/healthz`，均需登录。登录后的所有 POST 请求需要 `X-CSRF-Token`，由 `/api/status` 返回。没有授权回调路由或事件接收 HTTP 路由。

| 方法与路径 | 功能 |
| --- | --- |
| `POST /auth/login` | 工作台登录，JSON `{"password":"..."}` |
| `POST /auth/feishu/start` | 创建设备授权，返回链接、用户确认码和有效期 |
| `POST /auth/feishu/poll` | 检查当前浏览器的授权状态；服务器控制实际轮询频率 |
| `POST /auth/logout` | 退出工作台 |
| `GET /api/status` | 用户授权、长连接/同步进程状态、队列和分页进度 |
| `GET /api/chats` | 缓存会话，支持 `q`、`offset`、`limit` |
| `GET /api/chats/{chat_id}/messages` | 缓存消息，支持 `thread_id`、`offset`、`limit` |
| `GET /api/approvals?topic=1` | 审批缓存；topic 为 `1/2/17/18` |
| `GET /api/approvals/{instance_code}` | 已保存的审批详情 |
| `GET /api/approval-reviews` | 最近审批检查与执行记录，支持 `limit`、`offset` |
| `GET /api/approval-reviews/{task_id}` | 单个任务的判断依据、提交与确认状态 |
| `GET /api/sync-runs` | 最近 30 次同步结果 |
| `POST /api/sync/chats` | 同步或继续会话列表 |
| `POST /api/sync/messages/{chat_id}` | 消息同步，JSON `{"days":7,"restart":false}` |
| `POST /api/sync/threads/{thread_id}` | 读取已知话题回复 |
| `POST /api/sync/approvals/{topic}` | 同步某一审批分类 |
| `POST /api/sync/instances/{instance_code}` | 读取已知审批详情 |
| `GET /api/openapi.json` | 登录后查看 OpenAPI 定义 |
| `GET /healthz` | 工作台数据库健康检查 |

分页同步 POST 的 JSON 最少为 `{}`；审批详情不要求请求体。后台同步正占用资源锁时，手动同步返回 409，可稍后重试。错误格式为 `{"error":{"code":"...","message":"..."}}`，不返回上游原始错误中的凭证。

## 开发与测试

```bash
uv sync --frozen
uv run ruff check .
node --check app/static/app.js
uv run pytest -q -m 'not integration'
```

使用真实的独立 PostgreSQL 运行集成测试，飞书 HTTP 服务及 WebSocket 帧均由测试模拟，不读取实际账号：

```bash
docker compose -f compose.test.yaml -p octopus-test up -d --wait
TEST_DATABASE_URL=postgresql+psycopg://octopus_test:test_only@localhost:55432/octopus_test uv run pytest -q
docker compose -f compose.test.yaml -p octopus-test down
```

测试库名称必须以 `_test` 结尾，测试会清理其中应用表。覆盖浏览器授权绑定、令牌加密、轮询退避、重复事件、分片重组、提交后应答、失败应答、任务恢复、个人数据隔离和原有分页同步。

本地开发时，分别将 `.env` 的 `DATABASE_URL` 和 `MIGRATION_DATABASE_URL` 指向本机可访问的运行账号与 owner 账号地址（宿主机不使用 Docker DNS `shared-postgres`），然后启动：

```bash
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload --port 8000 --no-access-log
# 另一个终端启动唯一的事件接收进程：
uv run python -m app.worker
```

不要与 Docker 的 `worker` 同时运行第二个接收器。服务器部署使用 HTTPS 的 `APP_BASE_URL` 和反向代理提供工作台访问；飞书连接仍是主动出站，不需要配置回调。

## 结构与接口依据

`device_auth.py` 管理设备授权；`security.py` 管理会话及加密凭证；`feishu.py` 封装数据 API、审批订阅与单次同意请求；`approval_review.py` 管理政策核验和持久化执行状态；`events.py` 管理事件收件箱及同步队列；`worker.py` 运行长连接和后台检查；`sync.py` 负责分页与事务；`main.py` 提供工作台接口。`migrations/` 保存版本化迁移，保留原账号和业务数据。

- [飞书官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)：当前锁定 `lark-oapi==1.7.5`，SDK 适配器使用的内部接口升级时需重新验证。
- [官方设备授权实现](https://github.com/larksuite/cli/blob/main/internal/auth/device_flow.go)：设备授权与 `/oauth/v3/token` 轮询、刷新。
- [官方 IM 事件身份定义](https://github.com/larksuite/cli/blob/main/events/im/register.go)：消息事件使用机器人身份。
- [官方个人审批事件订阅](https://github.com/larksuite/cli/blob/main/events/approval/register.go)：实例/任务订阅使用用户身份。
- [官方公开 API 定义](https://open.feishu.cn/api/tools/open/api_definition?protocol=meta&client_version=1.0.97)：审批列表为 `GET /open-apis/approval/v4/tasks`，详情为 `GET /open-apis/approval/v4/instances/detail`，不要替换为旧版 `tasks/query`。

没有消息发送、审批拒绝或面向浏览器的直接审批写接口；自动同意由后台已核验规则触发。
