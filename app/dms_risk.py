"""Explainable DMS risk assessment from read-only API evidence.

Low risk is deliberately narrow: a bounded ordinary UPDATE in a verified test
environment, a matching rollback UPDATE, and successful DMS prechecks. This is
a technical risk classification, never an approval or a promise of correctness.
No SQL, applicant prose, credentials, or API error messages are echoed back.
"""

import re

MAX_LOW_RISK_ROWS = 100
MAX_SQL_CHARS = 100_000
TEST_ENVIRONMENTS = {"dev", "test", "sit", "uat"}
ENVIRONMENTS = {
    "product": "生产", "dev": "开发", "pre": "预发", "test": "测试",
    "sit": "集成测试", "uat": "验收测试", "pet": "压测", "stag": "预备",
}
PLUGIN_NAMES = {
    "DC_COMMON": "普通数据变更", "DC_CHUNK": "无锁变更", "DC_CRON_CLEAR": "历史数据清理",
    "DC_PROC": "存储过程变更", "DC_BIG_FILE": "数据导入", "DATA_EXPORT": "数据导出",
    "DB_EXPORT": "数据库导出", "PERM_APPLY": "权限申请",
}
REQUIRED_PRECHECKS = {"SQL_PARSE", "SQL_TYPE_CHECK", "PERMISSION_CHECK", "ROW_CHECK"}
PERMISSIONS = {
    1: "查询", 2: "导出", 3: "查询、导出", 4: "变更", 5: "查询、变更",
    6: "导出、变更", 7: "查询、导出、变更", 8: "登录",
}
SENSITIVE_FIELD = re.compile(r"password|passwd|secret|token|credential|private_?key|access_?key|permission|privilege|(?:^|_)role(?:_|$)", re.I)
TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>/\*[\s\S]*?\*/|--[^\r\n]*|\#[^\r\n]*)"
    r"|(?P<string>'(?:[^'\\]|'')*')|(?P<quoted>`[A-Za-z_][A-Za-z0-9_]*`)"
    r"|(?P<number>-?[0-9]+(?:\.[0-9]+)?)|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<symbol>[=,.;()])"
)


def _text(value, limit=80):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"https?://\S+", "", value)
    value = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _number(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,15}", value):
        return int(value)
    return value if type(value) is int and 0 <= value < 10**15 else None


def _list(value, wrapper):
    if isinstance(value, dict):
        value = value.get(wrapper)
    return value if isinstance(value, list) else []


def _text_sql(value):
    # The official enum is TEXT while the API example uses lowercase "text".
    return isinstance(value, str) and value.upper() == "TEXT"


def _result(facts, risks, missing):
    risks, missing = list(dict.fromkeys(risks)), list(dict.fromkeys(missing))
    low = not risks and not missing
    plugin_name = facts["工单类型"]
    if low:
        reasons = [
            "所有目标均为已标识的开发或测试环境。",
            f"单条 UPDATE 使用等值条件限定，DMS 逐库预检通过且合计影响不超过 {MAX_LOW_RISK_ROWS} 行。",
            "已提供对应回滚 SQL，未跳过预校验；仍需核实业务值和回滚值。",
        ]
        summary = f"{plugin_name}：{facts.get('环境', '测试')}环境，修改 {facts['变更对象']}，{facts['预计影响行数']}；判断为低危。"
    elif risks:
        reasons = risks[:6]
        operation = f"{facts['变更类型']} {facts['目标表']}；" if facts.get("目标表") else ""
        checked = f"系统校验 {facts['系统校验行数']} 行；" if facts.get("系统校验行数") else ""
        summary = f"{plugin_name}：{operation}{checked}高危。{risks[0]}"
    else:
        reasons = ["关键证据不足，暂按高危处理；这不代表已确认存在破坏性操作。"]
        summary = f"{plugin_name}：高危（信息不足，待核查）。{missing[0]}"
    return {
        "status": "ready" if low or risks else "unavailable",
        "risk": "low" if low else "high", "summary": summary,
        "reasons": reasons, "missing_evidence": missing[:8], "facts": facts,
    }


def _tokens(sql):
    """Reject unsupported syntax, including comments, rather than guessing SQL mode."""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL_CHARS:
        return None
    tokens, offset = [], 0
    while offset < len(sql):
        match = TOKEN.match(sql, offset)
        if match is None or match.lastgroup == "comment":
            return None
        if match.lastgroup != "space":
            tokens.append((match.lastgroup, match.group()))
        offset = match.end()
    if tokens and tokens[-1] == ("symbol", ";"):
        tokens.pop()
    return tokens


def _update(tokens):
    """Recognize one literal UPDATE with AND-ed equality predicates, not general SQL."""
    if not tokens:
        return None
    position = 0

    def take(kind=None, value=None):
        nonlocal position
        if position >= len(tokens):
            raise ValueError
        token_kind, token = tokens[position]
        if kind and token_kind != kind or value and token.upper() != value:
            raise ValueError
        position += 1
        return token_kind, token

    def identifier():
        kind, value = take()
        if kind not in {"word", "quoted"} or len(value.strip("`")) > 64:
            raise ValueError
        return value.strip("`")

    def scalar():
        kind, value = take()
        if kind in {"string", "number"}:
            return kind, value
        if kind == "word" and value.upper() in {"NULL", "TRUE", "FALSE"}:
            return kind, value.upper()
        raise ValueError

    def pairs(separator):
        values = {}
        while True:
            name = identifier()
            if name in values:
                raise ValueError
            take("symbol", "=")
            values[name] = scalar()
            if position == len(tokens) or tokens[position][1].upper() != separator:
                return values
            take(value=separator)

    try:
        take("word", "UPDATE")
        table = [identifier()]
        if position < len(tokens) and tokens[position] == ("symbol", "."):
            take("symbol", ".")
            table.append(identifier())
        take("word", "SET")
        assignments = pairs(",")
        take("word", "WHERE")
        predicates = pairs("AND")
        if position != len(tokens) or assignments.keys() & predicates.keys():
            return None
        return {"table": table, "assignments": assignments, "predicates": predicates}
    except ValueError:
        return None


def _sql_danger(sql):
    # Tokenization excludes words in literals. Unsupported syntax remains high
    # through missing evidence, so executable comments cannot bypass the gate.
    tokens = _tokens(sql)
    if not tokens:
        return []
    words = {value.upper() for kind, value in tokens if kind == "word"}
    reasons = []
    if words & {"DROP", "TRUNCATE", "ALTER", "CREATE", "RENAME"}:
        reasons.append("SQL 涉及删除或修改数据库结构，可能造成不可逆影响或锁表。")
    if words & {"DELETE", "REPLACE"}:
        reasons.append("SQL 涉及删除或替换现有数据，需要人工核验恢复方案。")
    if words & {"GRANT", "REVOKE"}:
        reasons.append("SQL 涉及权限变更，需要人工核验授权范围。")
    if "UPDATE" in words and "WHERE" not in words:
        reasons.append("UPDATE 没有 WHERE 条件，可能更新整张表。")
    if any(token == ("symbol", ";") for token in tokens):
        reasons.append("申请包含多条 SQL，需逐条核验执行顺序和相互影响。")
    return reasons


def _checked_sql_facts(checks):
    """Project verified API SQL metadata without expanding the low-risk grammar."""
    kinds, tables = set(), []
    for check in checks:
        kind = check.get("SqlType")
        if kind not in {"UPDATE", "INSERT", "DELETE", "REPLACE", "SELECT", "ALTER", "CREATE", "DROP", "TRUNCATE", "RENAME"}:
            return {}
        kinds.add(kind)
        names = check.get("TableNames")
        if not isinstance(names, str) or not names or len(names) > 4096:
            return {}
        for name in names.split(","):
            name = name.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}(?:\.[A-Za-z_][A-Za-z0-9_]{0,63})?", name):
                return {}
            if name not in tables:
                tables.append(name)
    if not kinds or not tables:
        return {}
    target = "、".join(tables[:3]) + (" 等" if len(tables) > 3 else "")
    return {"变更类型": "、".join(sorted(kinds)), "目标表": target, "变更对象": target}


def assess_dms_detail(snapshot):
    """Return bounded presentation facts; incomplete evidence never becomes low risk."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    base = snapshot.get("base") if isinstance(snapshot.get("base"), dict) else {}
    detail = snapshot.get("detail") if isinstance(snapshot.get("detail"), dict) else {}
    order = detail.get("OrderDetail") if isinstance(detail.get("OrderDetail"), dict) else {}
    plugin = base.get("PluginType")
    plugin = plugin if isinstance(plugin, str) else ""
    plugin_name = PLUGIN_NAMES.get(plugin, "待核实")
    order_id = str(snapshot.get("order_id", ""))
    valid_identity = bool(re.fullmatch(r"[0-9]{1,32}", order_id))
    valid_identity = valid_identity and str(base.get("OrderId", "")) == order_id
    facts = {"工单类型": plugin_name}
    if re.fullmatch(r"[0-9]{1,32}", order_id):
        facts["工单号"] = order_id
    if applicant := _text(base.get("Committer")):
        facts["申请人"] = applicant
    missing, risks = [], []
    if not valid_identity:
        missing.append("工单编号与详情响应的对应关系尚未核实。")
    if snapshot.get("complete") is not True or snapshot.get("errors"):
        missing.append("详情或 SQL 预检结果未完整取得。")
    if isinstance(snapshot.get("errors"), list) and "dms_order_reference_unverified" in snapshot["errors"]:
        missing.insert(0, "卡片 taskId 与 DMS 工单编号映射尚未核实。")
    if isinstance(snapshot.get("errors"), list) and "dms_order_type_unverified" in snapshot["errors"]:
        missing.insert(0, "卡片类型与 DMS 工单类型映射尚未核实。")
    if not base or not detail:
        missing.append("缺少工单基本信息或类型对应的详情。")
    if plugin in {"PERM_APPLY", "DATA_EXPORT", "DB_EXPORT"}:
        risks.append(
            "权限申请会改变数据库访问范围，需核验授权对象、范围和有效期。"
            if plugin == "PERM_APPLY" else "数据导出可能扩大数据暴露范围，需核验敏感信息和用途。"
        )
    elif plugin in {"DC_CRON_CLEAR", "DC_PROC", "DC_BIG_FILE"}:
        risks.append("该工单涉及数据清理、存储过程或批量导入，执行影响需人工核验。")
    elif plugin != "DC_COMMON":
        missing.append("当前低危规则仅覆盖普通数据变更，尚未核实该工单类型的完整影响。")

    databases = _list(detail.get("DatabaseList", order.get("DatabaseList")), "Database")
    if plugin == "PERM_APPLY":
        resources = _list(detail.get("Resources"), "Resource")
        databases = [
            resource.get("DatabaseInfo", resource.get("InstanceInfo", {}))
            for resource in resources if isinstance(resource, dict)
        ]
        facts["授权权限"] = PERMISSIONS.get(_number(detail.get("PermType")), "待核实")
        facts["授权范围"] = {
            "DB": "数据库", "TAB": "表", "COL": "列", "INSTANT": "实例", "ROW": "行",
        }.get(str(detail.get("ApplyType", "")), "待核实")
        duration = _number(detail.get("Seconds"))
        facts["授权时长"] = f"{duration} 秒" if duration is not None and duration > 0 else "待核实"
        if not resources or duration is None or duration <= 0 or facts["授权权限"] == "待核实":
            missing.append("授权资源、权限类型或有效期尚未完整核实。")
    elif plugin in {"DATA_EXPORT", "DB_EXPORT"}:
        databases = [{"DbId": order.get("DbId"), "EnvType": order.get("EnvType"), "SearchName": order.get("Database")}]
        facts["变更类型"] = "数据导出"
    valid_databases = bool(databases) and all(isinstance(db, dict) for db in databases)
    environments = {db.get("EnvType") for db in databases if isinstance(db, dict) and isinstance(db.get("EnvType"), str)}
    if environments:
        facts["环境"] = "、".join(ENVIRONMENTS.get(env, "未知") for env in sorted(environments))
    names = [_text(db.get("SearchName")) for db in databases if isinstance(db, dict)]
    if any(names):
        facts["数据库"] = "、".join(name for name in names[:5] if name) + (" 等" if len(names) > 5 else "")
    if "product" in environments and plugin not in {"PERM_APPLY", "DATA_EXPORT", "DB_EXPORT"}:
        risks.append("目标包含生产环境，数据变更可能影响线上业务。")
    if plugin != "DC_COMMON":
        if not valid_databases or not environments:
            missing.append("目标资源或环境归属尚未完整核实。")
        return _result(facts, risks, missing)
    if not valid_databases or any(
        not isinstance(db.get("EnvType"), str) or db.get("EnvType") not in TEST_ENVIRONMENTS | {"product"}
        for db in databases if isinstance(db, dict)
    ):
        missing.append("所有目标数据库的开发或测试环境归属尚未核实。")
    if order.get("IgnoreAffectRows") is True:
        risks.append("工单设置为忽略预校验，执行前的检查可能被跳过。")
    elif order.get("IgnoreAffectRows") is not False:
        missing.append("未确认是否跳过预校验。")

    estimate = _number(order.get("EstimateAffectRows"))
    if estimate is not None:
        facts["预计影响行数"] = str(estimate)
        if estimate > MAX_LOW_RISK_ROWS:
            risks.append(f"工单预计影响 {estimate} 行，超过低危上限 {MAX_LOW_RISK_ROWS} 行。")
    else:
        missing.append("缺少有效的预计影响行数。")
    checked_rows = _number(order.get("ActualAffectRows"))
    if checked_rows is not None:
        facts["系统校验行数"] = str(checked_rows)

    sql, rollback = order.get("ExeSQL"), order.get("RbSQL")
    risks.extend(_sql_danger(sql))
    if not _text_sql(order.get("SqlType")):
        missing.append("执行 SQL 不是已读取的完整文本，附件内容尚未核验。")
    forward_tokens, rollback_tokens = _tokens(sql), _tokens(rollback)
    forward, backward = _update(forward_tokens), _update(rollback_tokens)
    if forward:
        facts["变更类型"] = "单条 UPDATE（等值条件限定）"
        facts["目标表"] = ".".join(forward["table"])
        columns = list(forward["assignments"])
        facts["变更对象"] = facts["目标表"] + " 的 " + "、".join(columns[:5]) + (" 等字段" if len(columns) > 5 else " 字段")
        if len(forward["table"]) != 1:
            missing.append("SQL 指定了独立库或 schema，尚未核实其与目标数据库和测试环境的对应关系。")
        if any(SENSITIVE_FIELD.search(column) for column in forward["assignments"]):
            risks.append("SQL 修改凭据、权限或角色相关字段，需要人工核验安全影响。")
    else:
        facts["变更类型"] = "待人工核验"
        missing.append("执行 SQL 不符合单条、字面量赋值和等值 WHERE 条件的低危规则。")
    matching_rollback = bool(
        forward and backward and _text_sql(order.get("RbSQLType"))
        and backward["table"] == forward["table"]
        and backward["predicates"] == forward["predicates"]
        and backward["assignments"].keys() == forward["assignments"].keys()
        and backward["assignments"] != forward["assignments"]
    )
    no_rollback = rollback is None or isinstance(rollback, str) and not rollback.strip()
    facts["回滚"] = (
        "已提供同表、同条件、同字段回滚 SQL；未验证恢复结果" if matching_rollback
        else "未提供回滚 SQL" if no_rollback else "缺少可核验的对应回滚 SQL"
    )
    if no_rollback:
        missing.append("工单未提供回滚 SQL。")
    elif not matching_rollback:
        missing.append("回滚 SQL 未能与执行 SQL 的目标表、条件和修改字段对应。")

    prechecks = _list(detail.get("PreCheckDetail"), "TaskCheckDO")
    checked_steps = {step.get("CheckStep") for step in prechecks if isinstance(step, dict) and isinstance(step.get("CheckStep"), str)}
    prechecks_pass = bool(
        REQUIRED_PRECHECKS <= checked_steps
        and all(isinstance(step, dict) and step.get("CheckStatus") == "SUCCESS" for step in prechecks)
    )
    facts["预检"] = "解析、类型、权限、行数检查均通过" if prechecks_pass else "未取得完整通过结果"
    if not prechecks_pass:
        missing.append("缺少 SQL 解析、类型、权限、行数预检全部成功的结果。")
    if any(isinstance(step, dict) and step.get("CheckStatus") == "FAIL" for step in prechecks):
        risks.append("DMS 预检存在失败项。")

    checks = snapshot.get("sql_checks")
    checks = checks if isinstance(checks, list) else []
    database_ids = [_number(db.get("DbId")) for db in databases if isinstance(db, dict)]
    check_ids = [_number(check.get("DbId")) for check in checks if isinstance(check, dict)]
    check_rows = [_number(check.get("AffectRows")) for check in checks if isinstance(check, dict)]
    checks_complete = bool(
        valid_databases and checks and len(checks) == len(databases)
        and None not in database_ids and None not in check_ids
        and len(set(database_ids)) == len(database_ids) and sorted(check_ids) == sorted(database_ids)
        and len(check_rows) == len(checks) and None not in check_rows
        and all(
            isinstance(check, dict) and check.get("SqlReviewStatus") == "PASS"
            and forward_tokens and _tokens(check.get("CheckSQL")) == forward_tokens
            for check in checks
        )
    )
    if any(isinstance(check, dict) and check.get("SqlReviewStatus") == "BLOCK" for check in checks):
        risks.append("DMS SQL 审核存在阻断项。")
    if not checks_complete:
        missing.append("未取得与每个目标数据库、完整执行 SQL 对应的通过审核和影响行数。")
    else:
        total = sum(check_rows)
        facts["预计影响行数"] = f"预检合计 {total} 行；工单预计 {estimate if estimate is not None else '未知'} 行"
        if not forward and valid_identity and snapshot.get("complete") is True and not snapshot.get("errors"):
            facts.update(_checked_sql_facts(checks))
        if total > MAX_LOW_RISK_ROWS:
            risks.append(f"DMS 预检合计影响 {total} 行，超过低危上限 {MAX_LOW_RISK_ROWS} 行。")
        if estimate is not None and total > estimate:
            risks.append("DMS 预检影响行数超过工单填写的预计行数。")

    return _result(facts, risks, missing)
