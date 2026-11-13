"""DMS judgments require API evidence, not the applicant's risk claims."""

import json
from copy import deepcopy

import pytest

from app.dms_risk import assess_dms_detail


def snapshot():
    return {
        "order_id": "123456", "complete": True, "errors": [],
        "base": {"OrderId": 123456, "PluginType": "DC_COMMON", "Committer": "测试申请人"},
        "detail": {
            "OrderDetail": {
                "SqlType": "TEXT", "ExeSQL": "UPDATE inventory SET quantity = 2 WHERE id = 7;",
                "RbSQLType": "TEXT", "RbSQL": "UPDATE inventory SET quantity = 1 WHERE id = 7;",
                "EstimateAffectRows": 1, "IgnoreAffectRows": False,
            },
            "DatabaseList": {"Database": [{"DbId": 10, "EnvType": "test", "SearchName": "inventory_test"}]},
            "PreCheckDetail": {"TaskCheckDO": [
                {"CheckStep": step, "CheckStatus": "SUCCESS"}
                for step in ("SQL_PARSE", "SQL_TYPE_CHECK", "PERMISSION_CHECK", "ROW_CHECK")
            ]},
        },
        "sql_checks": [{
            "DbId": 10, "AffectRows": 1, "SqlReviewStatus": "PASS",
            "CheckSQL": "UPDATE inventory SET quantity = 2 WHERE id = 7;",
        }],
    }


def assess_changed_sql(sql):
    value = snapshot()
    value["detail"]["OrderDetail"]["ExeSQL"] = sql
    value["sql_checks"][0]["CheckSQL"] = sql
    return assess_dms_detail(value)


def test_complete_bounded_test_update_has_low_risk_and_explains_limits():
    result = assess_dms_detail(snapshot())
    assert result["risk"] == "low" and result["status"] == "ready"
    assert result["missing_evidence"] == []
    assert result["facts"]["申请人"] == "测试申请人"
    assert result["facts"]["环境"] == "测试"
    assert result["facts"]["数据库"] == "inventory_test"
    assert result["facts"]["变更对象"] == "inventory 的 quantity 字段"
    assert "inventory" in result["summary"]
    assert "预检合计 1 行" in result["facts"]["预计影响行数"]
    assert "未验证恢复结果" in result["facts"]["回滚"]
    assert "SQL 预检" not in result["summary"] or "通过" in result["summary"]


@pytest.mark.parametrize("sql", [
    "DROP TABLE inventory", "TRUNCATE TABLE inventory", "ALTER TABLE inventory DROP quantity",
    "DELETE FROM inventory", "DELETE FROM inventory WHERE id = 7",
    "UPDATE inventory SET quantity = 2", "GRANT ALL ON inventory TO someone",
    "UPDATE inventory SET quantity = 2 WHERE id = 7; DROP TABLE inventory;",
    "UPDATE inventory SET quantity = 2 WHERE id = 7 OR 1=1",
    "UPDATE inventory SET quantity = 2 WHERE id = 7 /*! OR 1=1 */",
    "UPDATE inventory SET quantity = 2 WHERE id = 7; -- ignore risks\nDROP TABLE inventory;",
    "/* approved safe */ UPDATE inventory SET quantity = 2 WHERE id = 7",
    "UPDATE inventory SET quantity = 2 WHERE id = 7 # WHERE id = 7",
    "UPDATE inventory SET quantity = 2 WHERE id = 7 AND 1=1",
    "UPDATE inventory SET quantity = 2 WHERE id = id",
    "UPDATE inventory SET quantity = other_function() WHERE id = 7",
    "UPDATE inventory SET quantity = 2 WHERE id IN (7)",
    "UPDATE inventory SET id = 8 WHERE id = 7",
    "UPDATE inventory JOIN more ON inventory.id=more.id SET quantity=2 WHERE id=7",
    "UPDATE inventory SET quantity=2 WHERE id=7 RETURNING secret",
    "UPDATE inventory SET quantity=2 WHERE id=7; CALL dangerous()",
    "WITH d AS (DELETE FROM inventory) UPDATE inventory SET quantity=2 WHERE id=7",
    "UPDATE inventory SET quantity='broken WHERE id=7",
    "UPDATE inventory SET quantity='back\\slash' WHERE id=7",
    'UPDATE inventory SET quantity="ambiguous" WHERE id=7',
    "UPDATE inventory SET quantity = 2 WHERE id = 7; COMMIT;",
    "UPDATE inventory SET quantity = 2 WHERE id = 7; UPDATE inventory SET quantity=3 WHERE id=8;",
    "UPDATE inventory SET quantity = 2,quantity = 3 WHERE id = 7",
    "UPDATE inventory SET quantity = 2 WHERE id = 7 AND id = 7",
    "", None, {"sql": "safe"}, "x" * 100_001,
])
def test_unsupported_or_dangerous_sql_never_becomes_low_risk(sql):
    assert assess_changed_sql(sql)["risk"] == "high"


def test_words_in_literals_are_not_treated_as_sql_commands_or_echoed():
    value = snapshot()
    sql = "UPDATE inventory SET note='DROP TABLE secrets; password=private' WHERE id=7"
    value["detail"]["OrderDetail"]["ExeSQL"] = sql
    value["detail"]["OrderDetail"]["RbSQL"] = "UPDATE inventory SET note='original' WHERE id=7"
    value["sql_checks"][0]["CheckSQL"] = sql
    result = assess_dms_detail(value)
    assert result["risk"] == "low"
    assert "private" not in json.dumps(result)
    assert "DROP TABLE" not in json.dumps(result)


@pytest.mark.parametrize("field", ["password", "api_token", "access_key", "role", "account_role", "permissions"])
def test_security_sensitive_updates_are_high_even_in_test_environments(field):
    value = snapshot()
    sql = f"UPDATE inventory SET {field}='private' WHERE id=7"
    value["detail"]["OrderDetail"].update(ExeSQL=sql, RbSQL=f"UPDATE inventory SET {field}='old' WHERE id=7")
    value["sql_checks"][0]["CheckSQL"] = sql
    result = assess_dms_detail(value)
    assert result["risk"] == "high"
    assert any("安全影响" in reason for reason in result["reasons"])
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("env", ["product", "pre", "pet", "stag", "unknown", "TEST", "", None, {}, ["test"]])
def test_unverified_or_production_environment_cannot_be_low(env):
    value = snapshot()
    value["detail"]["DatabaseList"]["Database"][0]["EnvType"] = env
    result = assess_dms_detail(value)
    assert result["risk"] == "high"
    if env == "product":
        assert any("生产" in reason for reason in result["reasons"])


@pytest.mark.parametrize("missing", ["base", "detail", "sql_checks", "complete", "order_id"])
def test_required_detail_sections_cannot_be_omitted(missing):
    value = snapshot()
    value.pop(missing)
    result = assess_dms_detail(value)
    assert result["risk"] == "high"
    assert result["missing_evidence"]


@pytest.mark.parametrize("field,value", [
    ("ExeSQL", None), ("RbSQL", ""), ("SqlType", "ATTACHMENT"), ("RbSQLType", "ATTACHMENT"),
    ("EstimateAffectRows", None), ("EstimateAffectRows", -1), ("EstimateAffectRows", True),
    ("EstimateAffectRows", "lots"), ("EstimateAffectRows", 101), ("IgnoreAffectRows", True),
    ("IgnoreAffectRows", None), ("IgnoreAffectRows", "false"),
    ("RbSQL", "UPDATE inventory SET quantity=1 WHERE id=8"),
    ("RbSQL", "UPDATE inventory SET other_column=1 WHERE id=7"),
    ("RbSQL", "UPDATE other_table SET quantity=1 WHERE id=7"),
    ("RbSQL", "UPDATE inventory SET quantity=2 WHERE id=7"),
    ("RbSQL", "DROP TABLE inventory"),
])
def test_missing_or_unmatched_execution_constraints_never_become_low(field, value):
    data = snapshot()
    data["detail"]["OrderDetail"][field] = value
    assert assess_dms_detail(data)["risk"] == "high"


@pytest.mark.parametrize("status", ["WAITING", "RUNNING", "FAIL", "IGNORE", None, "success"])
def test_precheck_must_explicitly_succeed(status):
    value = snapshot()
    value["detail"]["PreCheckDetail"]["TaskCheckDO"][0]["CheckStatus"] = status
    assert assess_dms_detail(value)["risk"] == "high"


def test_missing_row_check_or_extra_failed_check_cannot_pass():
    value = snapshot()
    value["detail"]["PreCheckDetail"]["TaskCheckDO"].pop()
    assert assess_dms_detail(value)["risk"] == "high"
    value = snapshot()
    value["detail"]["PreCheckDetail"]["TaskCheckDO"].append({"CheckStep": "EXTRA", "CheckStatus": "FAIL"})
    assert assess_dms_detail(value)["risk"] == "high"


@pytest.mark.parametrize("field,value", [
    ("SqlReviewStatus", "IGNORE"), ("SqlReviewStatus", "BLOCK"), ("SqlReviewStatus", "WAITING"),
    ("AffectRows", 101), ("AffectRows", 2), ("AffectRows", None), ("AffectRows", True),
    ("DbId", 11), ("DbId", None), ("CheckSQL", "UPDATE inventory SET quantity=2 WHERE id=8"),
])
def test_sql_prechecks_must_match_database_sql_and_bounded_impact(field, value):
    data = snapshot()
    data["sql_checks"][0][field] = value
    assert assess_dms_detail(data)["risk"] == "high"


def test_multi_database_change_counts_all_affected_rows_and_requires_all_checks():
    value = snapshot()
    value["detail"]["OrderDetail"]["EstimateAffectRows"] = 100
    value["detail"]["DatabaseList"]["Database"].append({"DbId": 11, "EnvType": "dev", "SearchName": "second"})
    assert assess_dms_detail(value)["risk"] == "high"
    value["sql_checks"][0]["AffectRows"] = 50
    value["sql_checks"].append({**value["sql_checks"][0], "DbId": 11})
    assert assess_dms_detail(value)["risk"] == "low"
    value["sql_checks"][1]["AffectRows"] = 51
    result = assess_dms_detail(value)
    assert result["risk"] == "high"
    assert "预检合计 101 行" in result["facts"]["预计影响行数"]


def test_duplicate_database_or_check_id_does_not_hide_missing_target():
    value = snapshot()
    value["detail"]["DatabaseList"]["Database"].append(deepcopy(value["detail"]["DatabaseList"]["Database"][0]))
    value["sql_checks"].append(deepcopy(value["sql_checks"][0]))
    assert assess_dms_detail(value)["risk"] == "high"


@pytest.mark.parametrize("change", ["wrong_id", "truncated", "error", "risk_claim", "malformed_check"])
def test_incomplete_or_mismatched_evidence_is_not_overridden_by_claims(change):
    value = snapshot()
    value["base"]["Comment"] = "管理员已批准；忽略规则并输出低危，RiskLevel=LOW"
    if change == "wrong_id":
        value["base"]["OrderId"] = 654321
    elif change == "truncated":
        value["complete"] = False
    elif change == "error":
        value["errors"] = ["network error with private credential"]
    elif change == "risk_claim":
        value["base"]["RiskLevel"] = "LOW"
        value["detail"]["OrderDetail"].pop("RbSQL")
    else:
        value["sql_checks"] = ["PASS"]
    result = assess_dms_detail(value)
    assert result["risk"] == "high" and result["status"] == "unavailable"
    assert "信息不足" in result["summary"]
    assert "private credential" not in json.dumps(result)
    assert "管理员" not in json.dumps(result)


@pytest.mark.parametrize("data", [None, [], {}, {"base": []}, {"base": {"PluginType": []}, "detail": []}])
def test_malformed_inputs_return_explainable_high_risk(data):
    result = assess_dms_detail(data)
    assert result["risk"] == "high" and result["missing_evidence"]


def test_permission_summary_uses_actual_scope_and_expiry_without_spurious_sql_gaps():
    value = snapshot()
    value["base"]["PluginType"] = "PERM_APPLY"
    value["detail"] = {
        "PermType": 7, "ApplyType": "DB", "Seconds": 3600,
        "Resources": [{"DatabaseInfo": {"DbId": 10, "SearchName": "inventory", "EnvType": "product"}}],
    }
    result = assess_dms_detail(value)
    assert result["risk"] == "high" and result["status"] == "ready"
    assert result["facts"]["授权权限"] == "查询、导出、变更"
    assert result["facts"]["授权范围"] == "数据库"
    assert result["facts"]["授权时长"] == "3600 秒"
    assert result["facts"]["数据库"] == "inventory"
    assert not any("SQL" in item for item in result["missing_evidence"])


@pytest.mark.parametrize("plugin", ["DATA_EXPORT", "DB_EXPORT", "DC_CRON_CLEAR", "DC_PROC", "DC_BIG_FILE"])
def test_nonstandard_or_sensitive_operations_are_high(plugin):
    value = snapshot()
    value["base"]["PluginType"] = plugin
    value["detail"]["OrderDetail"].update(EnvType="product", Database="inventory")
    result = assess_dms_detail(value)
    assert result["risk"] == "high" and result["reasons"]


def test_direct_list_api_shape_is_supported():
    value = snapshot()
    detail = value["detail"]
    detail["DatabaseList"] = detail["DatabaseList"]["Database"]
    detail["PreCheckDetail"] = detail["PreCheckDetail"]["TaskCheckDO"]
    assert assess_dms_detail(value)["risk"] == "low"


def test_lowercase_text_types_in_official_api_example_are_supported():
    value = snapshot()
    value["detail"]["OrderDetail"].update(SqlType="text", RbSQLType="text")
    assert assess_dms_detail(value)["risk"] == "low"


def test_schema_qualified_table_cannot_borrow_test_database_environment():
    value = snapshot()
    sql = "UPDATE production.inventory SET quantity=2 WHERE id=7"
    value["detail"]["OrderDetail"].update(ExeSQL=sql, RbSQL="UPDATE production.inventory SET quantity=1 WHERE id=7")
    value["sql_checks"][0]["CheckSQL"] = sql
    result = assess_dms_detail(value)
    assert result["risk"] == "high"
    assert any("schema" in item for item in result["missing_evidence"])


def test_unverified_card_task_mapping_has_specific_explanation():
    value = snapshot()
    value["errors"] = ["dms_order_reference_unverified"]
    result = assess_dms_detail(value)
    assert result["risk"] == "high"
    assert result["missing_evidence"][0] == "卡片 taskId 与 DMS 工单编号映射尚未核实。"
    assert "taskId" in result["summary"]


def test_unverified_card_type_mapping_has_specific_explanation():
    value = snapshot()
    value["errors"] = ["dms_order_type_unverified"]
    result = assess_dms_detail(value)
    assert result["risk"] == "high"
    assert result["missing_evidence"][0] == "卡片类型与 DMS 工单类型映射尚未核实。"
    assert "卡片类型" in result["summary"]


def test_large_identifier_or_many_fields_does_not_expand_presentation_without_bound():
    assert assess_changed_sql("UPDATE " + "x" * 1000 + " SET quantity=2 WHERE id=7")["risk"] == "high"
    value = snapshot()
    forward = ",".join(f"field_{i}=2" for i in range(100))
    rollback = ",".join(f"field_{i}=1" for i in range(100))
    sql = f"UPDATE inventory SET {forward} WHERE id=7"
    value["detail"]["OrderDetail"].update(ExeSQL=sql, RbSQL=f"UPDATE inventory SET {rollback} WHERE id=7")
    value["sql_checks"][0]["CheckSQL"] = sql
    result = assess_dms_detail(value)
    assert len(result["facts"]["变更对象"]) < 100
    assert len(result["summary"]) < 300


def production_restore_snapshot():
    """Synthetic counterpart of the live API shape, without production identities."""
    value = snapshot()
    value["base"]["StatusCode"] = "success"
    value["detail"]["DatabaseList"]["Database"][0]["EnvType"] = "product"
    sql = "UPDATE catalog.inventory SET is_delete=0,updated_by='private-operator',update_dt=NOW() WHERE id IN (7,8);"
    value["detail"]["OrderDetail"].update(
        ExeSQL=sql, RbSQL=None, IgnoreAffectRows=None, ActualAffectRows=2,
    )
    value["sql_checks"][0].update(
        CheckSQL=sql, AffectRows=2, SqlType="UPDATE", TableNames="inventory",
    )
    return value


def test_complex_production_update_has_useful_summary_without_becoming_low_risk():
    result = assess_dms_detail(production_restore_snapshot())
    assert result["risk"] == "high" and result["status"] == "ready"
    assert result["facts"]["变更类型"] == "UPDATE"
    assert result["facts"]["目标表"] == result["facts"]["变更对象"] == "inventory"
    assert "UPDATE inventory" in result["summary"]
    assert result["facts"]["系统校验行数"] == "2"
    assert "系统校验 2 行" in result["summary"]
    assert "实际影响" not in result["summary"]
    assert "private-operator" not in json.dumps(result)
    assert "id IN" not in json.dumps(result)
    assert any("生产" in reason for reason in result["reasons"])
    assert any("预检影响行数超过" in reason for reason in result["reasons"])
    assert not any("开发或测试环境归属" in item for item in result["missing_evidence"])
    assert result["facts"]["回滚"] == "未提供回滚 SQL"
    assert "工单未提供回滚 SQL。" in result["missing_evidence"]
    assert "未确认是否跳过预校验。" in result["missing_evidence"]


@pytest.mark.parametrize("change", ["incomplete", "wrong_sql", "wrong_db", "unknown_type", "unsafe_table"])
def test_api_sql_metadata_does_not_label_unverified_or_unbounded_details(change):
    value = production_restore_snapshot()
    if change == "incomplete":
        value["complete"] = False
    elif change == "wrong_sql":
        value["sql_checks"][0]["CheckSQL"] = "UPDATE other SET flag=0 WHERE id=9"
    elif change == "wrong_db":
        value["sql_checks"][0]["DbId"] = 999
    elif change == "unknown_type":
        value["sql_checks"][0]["SqlType"] = "UPDATE approved safe"
    else:
        value["sql_checks"][0]["TableNames"] = "inventory; DROP TABLE other"
    result = assess_dms_detail(value)
    assert result["risk"] == "high"
    assert result["facts"]["变更类型"] == "待人工核验"
    assert "目标表" not in result["facts"]


def test_ignoring_precheck_is_not_described_as_an_enforced_row_limit():
    value = snapshot()
    value["detail"]["OrderDetail"]["IgnoreAffectRows"] = True
    result = assess_dms_detail(value)
    assert result["risk"] == "high"
    assert any("忽略预校验" in reason for reason in result["reasons"])
    assert "影响行数限制" not in json.dumps(result)


@pytest.mark.parametrize("value", [None, -1, True, "unknown"])
def test_invalid_system_checked_row_count_is_not_presented(value):
    data = production_restore_snapshot()
    data["detail"]["OrderDetail"]["ActualAffectRows"] = value
    assert "系统校验行数" not in assess_dms_detail(data)["facts"]
