# Octopus Assistant

Python 3.12 + FastAPI + PostgreSQL + Docker 的个人飞书提醒助手。默认只处理个人 @、当前审批和 DBA DMS 待办，私信汇报处理进度，对超时未响应的事项电话提醒；持续模型审批判断保留。普通会话与历史消息同步已停用。模型固定为 **`gpt-6-sol`，思考强度 `xhigh`**。自动回复他人尚未实现。

使用 **WebSocket 长连接接收事件 + OAuth 设备授权**，无需配置 HTTP 回调地址、公网域名、内网穿透或飞书授权重定向 URL。

## 启动

需要 Docker Engine / Docker Desktop、Docker Compose，以及用于生成配置的 Python 3。

```bash
python3 scripts/setup_env.py
uv sync --frozen
.venv/bin/python scripts/setup_model.py
# macOS: install a background service that starts on user login.
.venv/bin/python scripts/model_service.py install
# Other systems: keep this running with your service manager:
# .venv/bin/python -m app.codex_host
# Configure DATABASE_URL and MIGRATION_DATABASE_URL, then start local-infra first.
docker compose --profile maintenance run --rm --build migrate
docker compose up -d --build app worker
```

首次运行启动命令前，在 `.env` 填写 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 和共享数据库连接。配置脚本不会覆盖已有 `.env`；随机密钥仅写入文件，权限为 `0600`。

访问 `APP_BASE_URL`，默认 [http://localhost:8000](http://localhost:8000)，使用 `.env` 的 `APP_PASSWORD` 登录。点击“连接飞书”，再点击“打开飞书授权页面”，确认授权后回到工作台。页面会自动检测授权完成并开始检查提醒事项。

本机端口被占用时，同时修改 `APP_PORT` 和 `APP_BASE_URL`，例如 `8180` 和 `http://localhost:8180`。**这个地址用于访问工作台，不需要填进飞书回调配置。**

本项目运行两个常驻容器，PostgreSQL 由独立的 `local-infra` Compose 管理：

| 服务 | 职责 |
| --- | --- |
| `app` | 工作台、设备授权、读取接口和人工反馈；使用受限运行账号 |
| `worker` | 连接飞书 WSS、读取提醒来源、调用模型判断待办并执行已满足条件的审批，对超时未响应的待处理事项发起电话提醒；使用同一运行账号 |
| `migrate` | maintenance profile 下的一次性 Alembic 迁移；仅使用本应用 owner 账号 |
| 宿主机 `app.codex_host` | 在本机独立运行 Codex 模型服务，复用 ChatGPT 登录，每次请求创建隔离的临时线程 |

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
| `im:chat:read` | 核验提醒来源会话 |
| `im:message:readonly` | 读取目标消息与响应状态 |
| `search:message` | 独立检索最近 @ 本人的消息 |
| `im:message.p2p_msg:get_as_user` | 个人单聊消息 |
| `im:message.group_msg:get_as_user` | 个人群聊消息 |
| `approval:task:read` | 待办、已办和知会列表；任务变化订阅 |
| `approval:instance:read` | 审批详情；实例变化订阅 |
| `approval:task:write` | 按已核验规则同意本人待办；没有此授权时仍可读取和检查 |

工作台按 `.env` 的 `FEISHU_SCOPES` 请求授权。更新权限需要发布应用并重新授权。单聊/群聊及审批的实际可见范围仍由飞书和组织权限决定。首次授权后会固定绑定账号和租户；可提前用 `FEISHU_ALLOWED_OPEN_ID` 限制账号。

## 持续检查与自动审批

`APPROVAL_AUTOMATION_ENABLED=true` 时，worker 独立检查待审批。相同任务两次检查至少间隔 60 秒，不受历史消息队列阻塞。工作台的审批列表、详情和连接页会展示判断原因、规则版本、提交与确认结果。

后台固定调用 `gpt-6-sol / xhigh`，不自动切换模型或降低思考强度。审批完整内容、任务身份、本人补充信息版本和提示词版本共同决定缓存；相同内容复用已保存结果，内容变化才重新判断。模型只返回结构化判断、理由、问题和引用依据，不持有飞书凭证或审批工具。

模型可以建议低风险且依据完整的申请通过。涉及资金、合同、人事、生产变更、破坏性操作、敏感权限或未读取附件时，程序要求本人介入。模型出错、证据无法核对或读取状态失败时保留待办；单个输入最多自动尝试三次并退避。工作台“审批”详情中会展示问题和依据，可以“提交补充信息”让模型继续判断、“确认通过”授权当前单据，或“暂不处理”。本人同意仅绑定当前任务及完整内容快照，不扩展到其他单据；暂缓后等待新的本人指示。

在宿主机安装并登录 Codex CLI **0.163.0**，运行 `.venv/bin/python scripts/setup_model.py`，再保持 `.venv/bin/python -m app.codex_host` 运行。服务会校验此版本的隔离协议，升级 CLI 后需要重新验证。模型服务默认监听本机 `8181`，Docker worker 使用 `host.docker.internal:8181` 访问。`APPROVAL_MODEL_ENABLED=false` 可以停用模型判断。设置脚本只在 `.data/` 创建私有运行目录和服务令牌，并链接本机已有的 Codex 登录凭证；不修改全局 Codex 配置。模型线程无环境访问，不加载用户技能、MCP 或项目指令；模型请求工具执行时直接失败。服务令牌只读挂载到 worker，登录凭证不进入容器或镜像。

macOS 可用 `scripts/model_service.py install` 注册独立的当前用户 LaunchAgent，服务退出后自动恢复，用户登录时启动；`status` 查看进程，`stop` 停止并取消自动启动。Docker Desktop 和本机需要保持运行。模型服务、ChatGPT 登录或指定型号不可用时，不切换其他型号，工作台保留待处理状态。

`approval-policy.json` 仍可保存经过业务核验的精确通过条件，优先于模型。它只读挂载到容器，已排除 Git 和镜像构建上下文。首次运行 `scripts/setup_env.py` 会创建空政策，不覆盖既有文件。已核验规则和模型评估、人工反馈、实际执行结果分别保存在审计记录中。

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

实际提交前读取飞书待办和详情，确认任务属于绑定用户、节点仍待处理、支持 API 操作且权限齐全。模型判断结束后再次读取并比对完整快照；人工反馈也必须通过最新快照检查。当前只检查最新待办第一页能明确找到的任务；超过第一页的任务保留人工处理，不凭旧缓存提交。只支持“同意”，不自动拒绝或修改审批表单。

提交标记先写入 PostgreSQL，再发送一次 `POST /approval/v4/tasks/pass`。请求超时、错误或进程重启后，只回读状态，不自动重试提交。只有同一实例、同一任务、同一用户的任务状态返回 `APPROVED`，才显示“已确认通过”；实例完成或待办消失都不作为通过证据。结果不确定会持续显示“待核实”。

设置 `APPROVAL_AUTOMATION_ENABLED=false` 并重建容器可暂停检查和后续提交；已经发出的请求可能仍会完成。首次增加写权限后需在工作台重新授权。

## 待处理事项电话提醒

`STATUS_NOTIFICATIONS_ENABLED=true` 时，独立后台循环在读取到新审批、DMS 待办或明确 @ 本人的消息后，直接用机器人给绑定账号本人发一张状态卡片，无需等待 15 分钟。同一事项的后续状态更新原卡片，不逐条发新私信。卡片只显示事项名称、当前状态和查看入口；需要本人介入或发生异常时，最多补一句问题或原因。例如 DMS 显示“权限申请 #28562578 / 正在核验”，随后更新为“待你审批”或“已通过”。审批只有回读确认后才称已通过，模型建议、提交中和结果未确认分别展示。DMS 只汇报卡片状态，不自动审批。原生审批可直接打开申请，DMS 和 @ 消息打开原会话。

本人在飞书手动处理审批后，后台核对同一实例、同一任务和本人的明确结果，并更新原卡片为“已通过”“已拒绝”“已转交”或“本人节点已结束”。待办消失或整个流程结束不等于本人同意；本人节点通过也不代表整个流程已结束。手动处理的结果只用于状态展示，不伪造应用提交记录。

补卡等由假勤接入的**三方审批**只使用已有 `approval:task:read` **用户权限**，无需增加应用权限或重新授权。已发送状态卡片的申请离开当前待办后，在卡片可更新的 14 天内，按审批定义和最多最近 14 天的时间范围核对个人已办记录；每次查询受 `SYNC_PAGE_LIMIT` 限制。只保存与已跟踪任务、实例、本人及外部标识完全匹配的最小状态，不归档其他已办记录，不启动 `approvals:2` 全量同步。个人接口只提供“本人已处理”及“整体流程结果”，卡片因此显示“已处理 · 流程已通过”等状态，不将它伪装成本人的同意或拒绝操作。仍在当前待办、结果缺失或身份不符时不认定完成，也不会扩大时间范围或改用企业审批查询；三方结果仅供展示，不作为自动审批依据。接口定义见[飞书官方个人审批 API](https://open.feishu.cn/api/tools/open/api_definition?protocol=meta&client_version=1.0.97)中的 `GET /approval/v4/tasks`。

即时私信使用机器人消息的发送、读取和卡片更新权限，不依赖电话权限。首次启用仅建立历史基线；升级保留旧文字通知，不把历史事项批量补发为卡片。卡片消息 ID 和接收账号持久化，重启后仍更新原卡片；每次更新前核验该卡片确由本应用发给绑定账号。创建或更新前记录提交标记，写入结果不明时不自动重发或另建卡片。未尝试写入的临时核验失败可重试。通知独立于模型耗时运行，实际到达时间仍受提醒数据读取与飞书接口影响；电话等待期限与冷却规则保持不变，关闭即时私信也不关闭电话提醒。

飞书[卡片更新接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/patch.md)只支持 14 天内发送的卡片；原卡片撤回、过期或写入结果不明时，后台记录失败或未确认，不通过新发消息绕过限制。工作台显示投递成功、待处理、失败和未确认次数，`GET /api/status-notifications` 可查询具体错误与创建/更新动作。这里的次数包括原卡片更新，不等于新私信数量。

**迟到的 @ 消息**：首次发现时，原消息已超过 `ATTENTION_TIMEOUT_SECONDS`（默认 15 分钟），只建立静默记录，不补发“刚收到”或“服务恢复时超时”的私信。已排队的收悉通知发送前重新检查原消息时间，其他普通消息状态通知排队超过同一期限也会失效，权限恢复或重启不会重新激活它们。已实际提交的提醒、电话结果仍会回执；审批和 DMS 待办继续按各自处理状态提醒。

`ATTENTION_ENABLED=true` 后，后台统一检查需要本人介入的审批、配置的 DBA DMS 审批卡片和重要 @ 消息，默认全天运行，`ATTENTION_TIMEOUT_SECONDS=900`（15 分钟）。沿用 `gpt-6-sol / xhigh`、现有飞书长连接和定时读取，不增加 HTTP 回调。响应以具体事项的反馈或处理状态为准，不读取工作台活跃度，也不声称能够监控你是否打开了飞书客户端。

**审批介入**：审批模型返回 `needs_human` 后，从首次提出人工介入开始等待。15 分钟后仍无本人反馈、任务仍待本人处理时，才进入电话提醒核验。这类提醒不依赖消息已读状态；仅打开工作台或审批详情不会取消提醒。在审批详情“提交补充信息”“确认通过”“暂不处理”、在飞书处理审批，或在提醒记录中点击“停止本次提醒”，都会取消本轮。“停止本次提醒”不会通过或拒绝审批，审批仍需本人处理。

同一审批任务最多发送一次提醒、提交一次电话请求。尚未发送提醒时，审批快照变化会更新问题并重新计时 15 分钟；尚未提交的审批提醒在服务重启后继续核验，保留原等待期限和全局冷却时间。已经存在发信或电话提交标记时，保留原标记，内容变化或服务重启均不会导致重拨。工作台记录展示审批任务、实例、判断记录、待核验问题、等待起点及本轮响应期限，并可打开对应审批继续处理。

**重要 @ 消息**：后台从首次启用时间起观察已同步会话中**明确 @ 本人**的新消息，调用模型判断是否重要且需要本人及时响应。到期后实时核对原消息、本人已读状态和后续会话，原消息所属话题内的回复也会单独核验。已读、本人在该会话后续发言或工作台点击“已处理”，都会停止升级；“已读”仅作为避免打扰的响应信号，不代表事项已完成。未知已读状态、分页未查全、消息变化或核验失败均不会触发电话。模型判断与电话执行独立，模型没有发信或呼叫工具。

**DBA DMS 卡片**：用 `DMS_SENDER_APP_ID` 和 `DMS_CHAT_ID` 固定可信 DBA 机器人及本人私聊，后台优先同步该会话。明确待处理的卡片从首次发现开始等待 15 分钟；当前仅支持可用的“同意 / 拒绝”按钮组合，不调用模型决定是否通过，不点击按钮或调用 DMS 审批接口。发送提醒前和拨号前都会重新读取原卡片。已完成、已撤销或本人点击“停止本次提醒”会停止提醒；已读和聊天回复不表示审批完成。同一 DMS 任务的重复通知不会重置等待或导致重拨，未提交的提醒在服务重启后保留。卡片模板或状态无法确认时保留待核查记录，不触发电话；处理状态以飞书可读取的 DBA 卡片为依据。

触发时，应用机器人先单聊给**绑定账号本人**发送提醒，再对这条机器人消息调用飞书电话加急，电话打到飞书绑定手机号。接口要求只能加急机器人自己发送的消息，所以不会直接加急别人发来的原消息。电话提醒消耗企业短信/电话加急额度；机器人会话被折叠等飞书端条件可能影响实际触达。[官方电话加急说明](https://open.feishu.cn/document/server-docs/im-v1/buzz-messages/urgent_phone)

应用需在飞书后台开通并发布以下**应用身份权限**：

- `im:message:send_as_bot`：机器人发送提醒给本人。
- `im:message.urgent:phone`：对提醒发起电话加急。

这两项不是用户 OAuth 权限，不需要追加到 `FEISHU_SCOPES` 或重新进行个人授权。后台通过应用令牌的有效 scope 检查权限；权限不足时，工作台显示缺少的项。本人已读查询使用现有 `im:message:readonly` 用户权限。[官方 CLI 已读状态契约](https://github.com/larksuite/cli/blob/main/skills/lark-im/references/lark-im-message-read-status.md)

审批介入、DMS 卡片和重要 @ 消息共用 `ATTENTION_CALL_COOLDOWN_SECONDS=1800`，本人的任意两次电话尝试至少间隔 30 分钟。同一原消息最多提交一次电话请求。发信和电话请求前先保存提交标记；超时、错误或中途重启后不重复提交。状态“电话已受理”只表示飞书接受请求，不能证明电话已响铃或接听。恢复服务时不补打已经错过期限的旧 @ 消息；未提交的审批和 DMS 提醒会继续实时核验。记录统一保留在“连接与同步 → 待处理事项电话提醒”。

对于 @ 消息，发送提醒前后续讨论发生变化会重新交给模型判断。提醒已经发出后，如果讨论有新进展或最终核验失败，本条电话升级会停止，不再自动补打。

`ATTENTION_ENABLED=false` 并重建容器可以停用三类电话提醒；审批模型的 `APPROVAL_MODEL_ENABLED` 开关不影响消息重要性判断。审批介入提醒复用已有审批模型结论，不为电话提醒另做一次审批判断。

**当前无法可靠监听别人给本人的原生 Buzz。**已核对公开消息接口、事件列表和现有个人消息返回结构，尚未找到接收该类 Buzz 的公开契约，因此工作台明确显示此限制，不把普通消息当作 Buzz。个人 @ 消息通过用户 API 定时读取，实际发现和升级时间会受消息同步队列、模型耗时及飞书接口影响。

## 长连接和个人数据范围

飞书 `im.message.receive_v1` 是**机器人身份事件**，不会推送个人账号的所有聊天。因此应用同时采用：

- **长连接事件**：已配置且可投递的审批状态事件，以及机器人可见的消息变化。先将事件 ID 和资源标识写入 PostgreSQL，提交成功后才应答；重复推送幂等处理。
- **个人 @ 独立同步**：独立后台循环每 `max(60, SYNC_INTERVAL_SECONDS)` 秒用用户身份搜索最近 15 分钟 `@ 我` 的消息，批量读取正文，并核验 `mentions` 确实包含本人（搜索命中也可能包含 `@all`）。它使用独立 HTTP 客户端，不等待普通同步循环及其正在进行的请求；分页进度独立保存，短时重叠读取后按消息 ID 去重。停机恢复时检查当前窗口，不补录错过的历史消息；接口未承诺索引实时性，不能将 60 秒轮询理解为送达保证。
- **当前审批与 DMS**：每 `SYNC_INTERVAL_SECONDS` 秒检查待我审批和固定 DBA 会话的近期 DMS 卡片，读取当前待办及执行结果未确认的审批详情。已发送状态卡片的任务离开待办后，在卡片可更新的 14 天期限内定向核验最终结果，确认本人节点结果后停止这项补查；不扫描已办历史。DMS 使用独立的近期窗口，不能续跑旧会话的历史分页；已跟踪待办继续逐条核验。
- **仅提醒模式**：`REMINDERS_ONLY=true`（默认）禁止普通会话、普通消息和话题历史的后台及手动同步。启动时和运行中取消旧队列，事件或重启不会重新加入；不再轮询已办/已读知会历史。已有缓存和已发送提醒记录保留。工作台默认显示提醒记录。

事件只作为更新提示。消息正文、原生审批详情及三方审批的个人处理状态均通过**绑定账号的用户令牌**读取。机器人所在但本人不可见的聊天内容不会被直接导入个人工作台。附件目前展示类型和原始结构，不下载二进制内容。“我发起的审批”单独列表尚未提供。

提醒所需的网络读取在数据库事务之外执行，提交时才短暂加锁并核对进度与版本。发送提醒和拨号前，仍按需读取原消息、已读状态及后续回复；这不会启动普通会话归档。模型判断、飞书限流或索引延迟以及运行机器休眠仍会影响最终提醒时间。

个人 @ 搜索需要 **`search:message` 用户权限**。在自建应用后台开通并发布后，将它加入 `FEISHU_SCOPES`，重建应用和 worker，再在工作台点击“重新授权”；默认配置已包含它。缺少授权时，“@ 我的消息检查”显示等待授权；仅提醒模式不会退回全量会话轮询。[官方搜索接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/search.md)

SDK 处理 WSS 协议、分片与心跳；进程监督重连并限制获取连接地址的 HTTP 超时。数据库锁确保同一实例仅一个事件接收进程；启动连接前检查飞书远端在线数，检测到其他连接时显示“该应用已有其他长连接”，避免多接收器分流事件。

长连接断开期间不承诺飞书补发全部事件。定时检查仅读取提醒所需的当前事项，不提供完整聊天归档或停机期间的历史消息补录。

## 同步、授权和数据保存

- 页面读取本地缓存，并每 10 秒刷新状态。个人业务数据使用 `user_access_token` 读取；应用令牌用于长连接连接数、机器人权限检查，以及向本人发出提醒和电话加急。
- `SYNC_PAGE_LIMIT` 默认 5，控制每批最大页数。数据和分页游标在一个事务提交；失败回滚当前批，已完成批次保留。后台自动续传，有需要也可手动继续或重新开始。
- 消息按 `message_id` 幂等更新；审批按 `(task_id, topic)` 保存分类。完整同步结束后才隐藏未出现的旧列表成员。待办消失不会被推断为审批通过。
- 同步任务持久化，可在进程重启后继续；临时错误退避重试。事件去重记录保留 7 天，仅记录事件 ID 和资源标识，不保存事件消息正文。
- 会话与审批列表优先完成分页，随后优先处理近 30 分钟消息事件对应的会话和待审批详情，避免新数据排在大量历史任务之后。事件中的未知会话最多额外完整补查一次；失败任务仍遵守退避时间。
- 设备授权绑定工作台浏览器会话，遵守飞书轮询间隔、`slow_down`、拒绝和过期状态。设备码和换得的令牌加密保存，浏览器只收到授权链接及用户确认码。
- 用户访问/刷新令牌使用 Fernet 加密，刷新用行锁串行化。消息正文和审批内容存于普通数据库字段及 JSONB，数据库本身并非全库加密。请保留 `TOKEN_ENCRYPTION_KEY`。
- 退出工作台结束浏览器会话和未完成的设备授权，后台同步继续。停止后台同步可执行 `docker compose stop worker`；撤销用户授权需在飞书账号授权管理中操作。

## 平台研发服务清单

[平台研发团队服务清单](docs/platform-team-services.md) 从 Crane 完整分页读取应用和团队目录，按平台研发部（部门 ID `3`、标识 `platform`）的团队归属整理；包含所有登记类型，关联集群不代表当前正在运行。每次成功更新记录新增、移出和资料变更，查询失败或分页不完整时保留旧清单。

本机定时任务每天北京时间 **15:00** 更新项目内 Markdown，使用现有 `cockpit` 登录，不复制凭据，也不调用模型。安装要求 macOS 系统时区为 `Asia/Shanghai`；当前用户需保持登录、内网及 cockpit 认证可用，休眠错过时唤醒后补跑。该任务独立于 Docker 服务。

```bash
.venv/bin/python scripts/update_platform_services.py
.venv/bin/python scripts/platform_services_schedule.py install
.venv/bin/python scripts/platform_services_schedule.py status
# 停止定时更新：
.venv/bin/python scripts/platform_services_schedule.py stop
```

运行结果和日志保存在 `.data/platform-services/`，不会提交本地认证或运行快照。文档由脚本生成，团队归属应在 Crane 修改。

## 平台研发错误日志日报

本机任务每天北京时间 **16:00** 统计截至当日 16:00 的最近 24 小时，将 `cn-shanghai / pro-jd-sh-service / error` 日志记录的 `topic` 字段与平台研发服务清单精确匹配，按错误机制归并。服务归属使用当日 Crane 清单，缺失或过期时自动刷新。

每个错误类别附可复制的 TLS 查询语句、服务名与北京时间查询范围；有真实 swtid 时用它定位，否则使用日志中的错误特征。查询由 TLS 实际核验，定位信息不由模型生成。

报告在[ERROR LOG 治理目录](https://atrenew.feishu.cn/wiki/Z7FUwrWH3ioGtPk3o41cUqojnvg)下按 `YYYY-MM-DD 平台研发错误日志日报` 创建。TLS 聚合给出错误量；代表日志结合 Grafana 指标及 Crane 发布记录分析原因和具体处置建议。模型使用 **gpt-6-sol / xhigh**，没有工具或执行生产变更的权限；没有因果证据的根因标为待核实。查询缺口、样本截断及分析覆盖范围会在文档中说明。

```bash
# 执行最近已结束的 16:00 日报窗口；同日成功后不会重复创建
.venv/bin/python scripts/run_tls_error_report.py
# 补齐或修订已发布的当天报告，更新原文档
.venv/bin/python scripts/run_tls_error_report.py --republish
# 仅采集与交叉核验，不调用模型、发布或通知
.venv/bin/python scripts/run_tls_error_report.py --collect-only
# 补跑指定已结束的日期
.venv/bin/python scripts/run_tls_error_report.py --date 2026-10-21
.venv/bin/python scripts/tls_report_schedule.py install
.venv/bin/python scripts/tls_report_schedule.py status
.venv/bin/python scripts/tls_report_schedule.py stop
```

采集和分析任务运行在宿主机，私信通知复用 Docker 中现有 Octopus 应用机器人，需保持 `app` 容器运行。要求本机用户登录、系统时区为 `Asia/Shanghai`，且 `ve >= 1.1.11`、`cockpit`、`lark-cli`、`docker` 和 Codex 现有认证可用。Grafana 使用本地 `.codex/config.toml` 中已配置的服务账号；不复制凭据到报告或定时任务。电脑休眠时，唤醒后补跑最近一个已结束窗口，不承诺补齐关机期间的所有日期。

日报发布或更新成功后，机器人向绑定本人私信发送日期、统计及飞书文档链接；任务失败也发送简短状态。相同文档内容重复执行不重复提醒，失败后恢复成功可另行通知。通知失败不会抹掉文档已发布的状态，重跑只补通知；提交结果不明确时不盲目重发。

运行状态、脱敏证据、定位查询、模型结论、私信及发布记录位于 `.data/tls-error-reports/`；`last-run.json` 和 `schedule.stderr.log` 用于排查。单机进程锁避免并发，失败保留中间结果供重跑；飞书提交结果不明确时先核对远端，避免重复建文档。更新已有报告时只更新本任务创建且未被人工修改的内容。

### ve / TLS 登录态维护

独立的 macOS LaunchAgent 每 **30 分钟**执行一次轻量检查，用户登录或安装任务时也执行。先调用 `ve sts GetCallerIdentity --output off`，再调用 `ve volclog --region cn-shanghai doctor --online`，让官方 CLI 在需要时自动续期，并分别验证 ve 和 TLS 是否可用。任务不调用模型、不扫描错误日志，执行完退出；没有常驻轮询或失败后密集重试。

```bash
.venv/bin/python scripts/maintain_ve_session.py
.venv/bin/python scripts/ve_session_schedule.py install
.venv/bin/python scripts/ve_session_schedule.py status
.venv/bin/python scripts/ve_session_schedule.py stop
```

`ve` 与 `volclog` 的 Console Login 缓存相互独立：ve 续期失败时，TLS 仍可能正常查询。检查结果保存在 `.data/ve-session/last-run.json`，包含分别的状态、最近成功时间和 TLS 临时凭证到期时间；`healthy` 表示当次验证通过，`reauth_required` 表示需要恢复对应登录。仅保存经过筛选的状态，不读取或复制认证文件、AK/SK、token；定时任务沿用各 CLI 默认 profile。

这不能保证授权永不失效。[TLS 官方认证说明](https://github.com/volcengine-tls/ve-tls-cli/blob/b451c506f24573dc0e71220e7ad15339ed7f04e4/docs/2-Authentication_zh.md)规定有效期以服务端返回为准，临近到期时才自动刷新；授权过期、撤销或刷新令牌被拒绝后，仍需本人通过对应 CLI 的登录流程重新授权。后台不会自动发起交互登录、切换账号或修改认证缓存。电脑关机、休眠、退出当前用户或断网期间不能保证检查按时运行；恢复联网运行后下一次检查重试。维护检查本身不发私信；若影响日报，沿用日报失败通知。

## 接口

除了首页、静态文件、登录和 `/healthz`，均需登录。登录后的所有 POST 请求需要 `X-CSRF-Token`，由 `/api/status` 返回。没有授权回调路由或事件接收 HTTP 路由。

| 方法与路径 | 功能 |
| --- | --- |
| `POST /auth/login` | 工作台登录，JSON `{"password":"..."}` |
| `POST /auth/feishu/start` | 创建设备授权，返回链接、用户确认码和有效期 |
| `POST /auth/feishu/poll` | 检查当前浏览器的授权状态；服务器控制实际轮询频率 |
| `POST /auth/logout` | 退出工作台 |
| `GET /api/status` | 用户授权、长连接/同步进程状态、队列和分页进度 |
| `GET /api/chats` | 缓存会话，按 `last_activity_time`（最新已同步消息的创建/更新时间，毫秒）倒序；无消息时间的会话置后，支持 `q`、`offset`、`limit` |
| `GET /api/chats/{chat_id}/messages` | 缓存消息，支持 `thread_id`、`offset`、`limit` |
| `GET /api/approvals?topic=1` | 审批缓存；topic 为 `1/2/17/18` |
| `GET /api/approvals/{instance_code}` | 已保存的审批详情 |
| `GET /api/approval-reviews` | 最近审批检查与执行记录，支持 `limit`、`offset` |
| `GET /api/approval-reviews/{task_id}` | 单个任务的判断依据、提交与确认状态 |
| `POST /api/approval-reviews/{task_id}/respond` | 提交本人补充、确认或暂缓；包含 `assessment_id`、`snapshot_hash`、`action`（`answer/approve/hold`）和 `text` |
| `GET /api/attention` | 审批介入、DMS 卡片及重要消息的等待、响应核验和电话提醒记录；`source_type` 区分 `approval` / `dms_card` / `message` |
| `GET /api/status-notifications` | 本人的状态卡片投递记录，支持 `limit`、`offset`；`delivery_action` 区分创建/更新/历史文字，`unknown` 表示结果未确认，`failed` 表示无法投递 |
| `POST /api/attention/{message_id}/ack` | 以记录的唯一提醒 ID 停止本轮未提交的电话升级；审批记录对应“停止本次提醒”，不会通过或拒绝审批 |
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

消息发送仅用于机器人给绑定账号本人的待处理审批及重要消息提醒。浏览器提交人工反馈，后台重新检查状态后执行审批；接口本身不会直接调用飞书同意请求。尚未实现自动回复他人或审批拒绝。
