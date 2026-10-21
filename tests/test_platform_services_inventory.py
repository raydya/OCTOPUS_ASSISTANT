import json
from copy import deepcopy

import pytest

from scripts import update_platform_services as inventory


class CatalogClient:
    """An in-memory version of cockpit's zero-based directory pagination."""

    def __init__(self, teams=None, apps=None):
        self.catalogs = {"/api/v1/teams": teams or [], "/api/v1/apps": apps or []}
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        rows = self.catalogs[path]
        size, page = params["pageSize"], params["pageIndex"]
        return {
            "data": deepcopy(rows[page * size:(page + 1) * size]),
            "page": page,
            "pageSize": size,
            "totalCount": len(rows),
        }


class PageClient:
    def __init__(self, pages):
        self.pages = pages

    def get(self, path, params):
        return deepcopy(self.pages[params["pageIndex"]])


def team(team_id=9, name="支付基础设施", department_id=3, slug="platform"):
    return {
        "id": team_id,
        "name": name,
        "departmentID": department_id,
        "departmentSlug": slug,
        "departmentName": "平台研发部" if department_id == 3 else "其他部门",
        "maintainers": [{"name": "团队负责人", "employeeID": "secret-employee-id"}],
        "larkChatId": "private-chat-id",
    }


def app(app_id=1, owner=None, kind="service"):
    owner = owner or team()
    return {
        "id": app_id,
        "name": f"app-{app_id}",
        "team": owner["id"],
        "teamDetail": deepcopy(owner),
        "typeName": kind,
        "description": "业务服务",
        "techStack": "Python",
        "repoURL": "https://code.example.test/platform/app?token=private-query#private-fragment",
        "namespace": "platform",
        "clusters": ["uat", "dev", "dev"],
        "maintainers": [{"name": "应用负责人", "employeeID": "private-maintainer-id"}],
        "developers": [{"name": "开发人员", "employeeID": "private-developer-id"}],
        "larkWebhook": "https://hooks.example.test/private-hook-token",
        "accessToken": "private-access-token",
    }


def snapshot(teams=None, apps=None):
    result = inventory.build_inventory(teams or [team()], apps or [app()])
    result["updated_at"] = "2026-10-21T15:00:00+08:00"
    return result


def test_fetch_all_reads_beyond_cli_first_thousand_and_preserves_query():
    rows = [{"id": index} for index in range(1399)]
    client = CatalogClient(apps=rows)

    result = inventory.fetch_all(
        client, "/api/v1/apps", {"onlyMine": "false", "isShowDetail": "true"}
    )

    assert result == rows
    assert [params["pageIndex"] for _, params in client.calls] == list(range(7))
    assert all(params["pageSize"] == 200 for _, params in client.calls)
    assert all(params["onlyMine"] == "false" for _, params in client.calls)
    assert all(params["isShowDetail"] == "true" for _, params in client.calls)


@pytest.mark.parametrize("failure", ["duplicate", "total_changed", "short_page"])
def test_fetch_all_rejects_incomplete_or_changing_catalog(failure):
    first = {"data": [{"id": 1}, {"id": 2}], "page": 0, "pageSize": 2, "totalCount": 4}
    second = {"data": [{"id": 3}, {"id": 4}], "page": 1, "pageSize": 2, "totalCount": 4}
    if failure == "duplicate":
        second["data"] = [{"id": 1}, {"id": 2}]
    elif failure == "total_changed":
        second["totalCount"] = 5
    else:
        first["data"].pop()

    with pytest.raises(inventory.InventoryError):
        inventory.fetch_all(PageClient([first, second]), "/api/v1/apps", page_size=2)


@pytest.mark.parametrize(
    "change",
    [
        {"page": 1},
        {"page": False},
        {"totalCount": "1"},
        {"pageSize": 0},
        {"data": [{"id": True}]},
        {"data": [{"name": "missing-id"}]},
    ],
)
def test_fetch_all_requires_verifiable_pagination_and_ids(change):
    response = {"data": [{"id": 1}], "page": 0, "pageSize": 200, "totalCount": 1}
    response.update(change)

    with pytest.raises(inventory.InventoryError):
        inventory.fetch_all(PageClient([response]), "/api/v1/apps")


def test_build_inventory_uses_department_identity_and_keeps_every_app_type():
    platform = team(name="已更名的基础设施团队")
    unrelated = team(88, "平台研发-名称相似团队", department_id=6, slug="engineering")
    apps = [app(1, platform), app(2, platform, "consumer"), app(3, platform, "web")]
    apps.append(app(4, unrelated))

    result = inventory.build_inventory([unrelated, platform], apps)

    assert result["catalog_team_count"] == 2
    assert result["catalog_app_count"] == 4
    assert [item["id"] for item in result["teams"]] == [9]
    assert [item["id"] for item in result["services"]] == [1, 2, 3]
    assert {item["type"] for item in result["services"]} == {"service", "consumer", "web"}


@pytest.mark.parametrize("field,value", [("id", 10), ("departmentID", 6), ("departmentSlug", "other")])
def test_build_inventory_rejects_conflicting_app_ownership(field, value):
    record = app()
    record["teamDetail"][field] = value

    with pytest.raises(inventory.InventoryError):
        inventory.build_inventory([team()], [record])


def test_build_inventory_rejects_missing_or_unconfirmed_platform_department():
    with pytest.raises(inventory.InventoryError):
        inventory.build_inventory([team(department_id=6, slug="engineering")], [])
    with pytest.raises(inventory.InventoryError):
        inventory.build_inventory([team(slug="unexpected")], [])


def test_snapshot_and_markdown_only_contain_allowed_fields():
    data = snapshot()
    rendered = inventory.render_document(data)
    outputs = json.dumps(data, ensure_ascii=False) + rendered

    for forbidden in (
        "secret-employee-id", "private-chat-id", "private-maintainer-id",
        "private-developer-id", "private-hook-token", "private-access-token",
        "private-query", "private-fragment", "employeeID", "larkWebhook", "accessToken",
    ):
        assert forbidden not in outputs
    assert data["services"][0]["repository"] == "https://code.example.test/platform/app"
    assert data["services"][0]["clusters"] == ["dev", "uat"]
    assert "应用负责人" in rendered
    assert "开发人员" in rendered


@pytest.mark.parametrize(
    "unsafe",
    [
        "https://user:password@code.example.test/repo",
        "javascript:alert(1)",
        "file:///tmp/private",
        "https://code.example.test/repo\nforged-row",
    ],
)
def test_repository_url_drops_credentials_and_unsafe_links(unsafe):
    assert inventory.repository_url(unsafe) == ""


def test_markdown_escapes_table_delimiters_html_backticks_and_line_breaks():
    record = app()
    record["description"] = "a|b\n<script>unsafe</script> `code` & value"
    record["repoURL"] = "https://code.example.test/repo(with)paren"

    rendered = inventory.render_document(snapshot(apps=[record]))
    service_line = next(line for line in rendered.splitlines() if line.startswith("| app-1（"))

    assert "a&#124;b &lt;script&gt;unsafe&lt;/script&gt; &#96;code&#96; &amp; value" in service_line
    assert service_line.count("|") == 9
    assert "[仓库](<https://code.example.test/repo%28with%29paren>)" in service_line


def test_update_inventory_writes_complete_document_and_filtered_snapshot(tmp_path):
    document, runtime = tmp_path / "services.md", tmp_path / "runtime"
    client = CatalogClient(teams=[team()], apps=[app()])

    result = inventory.update_inventory(client, document=document, runtime=runtime)

    assert result["status"] == "updated"
    assert result["services"] == 1
    assert result["teams"] == 1
    assert document.read_text().startswith(inventory.MARKER)
    saved = json.loads((runtime / "snapshot.json").read_text())
    assert saved["updated_at"].endswith("+08:00")
    assert saved["services"][0]["id"] == 1
    assert client.calls[0][1]["includingUser"] == "true"
    assert client.calls[1][1]["onlyMine"] == "false"
    assert client.calls[1][1]["isShowDetail"] == "true"


@pytest.mark.parametrize("failure", ["network", "pagination", "ownership"])
def test_failed_update_preserves_last_good_document_and_snapshot(tmp_path, failure):
    document, runtime = tmp_path / "services.md", tmp_path / "runtime"
    runtime.mkdir()
    before_document = inventory.MARKER + "\nExisting successful inventory\n"
    before_snapshot = json.dumps(snapshot(), ensure_ascii=False)
    document.write_text(before_document)
    (runtime / "snapshot.json").write_text(before_snapshot)
    client = CatalogClient(teams=[team()], apps=[app()])
    original_get = client.get

    def failing_get(path, params):
        if path == "/api/v1/apps":
            if failure == "network":
                raise inventory.InventoryError("网络不可用")
            response = original_get(path, params)
            if failure == "pagination":
                response["totalCount"] = 20
            else:
                response["data"][0]["teamDetail"]["departmentSlug"] = "other"
            return response
        return original_get(path, params)

    client.get = failing_get
    with pytest.raises(inventory.InventoryError):
        inventory.update_inventory(client, document=document, runtime=runtime)

    assert document.read_text() == before_document
    assert (runtime / "snapshot.json").read_text() == before_snapshot


def test_update_refuses_to_overwrite_a_human_document(tmp_path):
    document = tmp_path / "services.md"
    document.write_text("An existing manually maintained document")
    client = CatalogClient(teams=[team()], apps=[app()])

    with pytest.raises(inventory.InventoryError, match="未覆盖"):
        inventory.update_inventory(client, document=document, runtime=tmp_path / "runtime")

    assert document.read_text() == "An existing manually maintained document"
    assert client.calls == []
