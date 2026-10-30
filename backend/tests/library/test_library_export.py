"""Single Markdown and whole-library ZIP exports: readable, complete, scoped."""
import hashlib
import io
import json
import os
import zipfile

from library_helpers import form_for


def unzip(response) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def by_suffix(files: dict[str, bytes], suffix: str) -> bytes:
    [name] = [name for name in files if name.endswith(suffix)]

    return files[name]


def delete(browser, item_id):
    response = browser.submit(f"/experiences/{item_id}/delete", f"/experiences/{item_id}/delete")
    assert response.status_code == 303


def test_single_export_is_readable_markdown_with_the_whole_record(browser_a):
    item_id = browser_a.create(
        title="校园二手书交换小程序",
        actions="负责后端接口",
        private_note="面试时不要说成独立完成",
    )
    response = browser_a.get(f"/experiences/{item_id}/export.md")
    text = response.content.decode("utf-8")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert text.startswith("# 校园二手书交换小程序\n")
    assert "- 状态：待核对" in text
    assert "**我的角色**\n\n未填写" in text
    assert "**做了什么**\n\n负责后端接口" in text
    assert "## 私有备注\n\n面试时不要说成独立完成" in text
    assert "不再受平台的权限保护" in text
    assert "## 历史版本（1 个）" in text
    assert "### 初始版" in text
    assert "导出范围" in text


def test_zip_export_has_readme_data_manifest_and_one_file_per_experience(browser_a):
    browser_a.create(title="项目一", actions="写了爬虫")
    browser_a.create(actions="只写了一句话")
    response = browser_a.get("/export/all.zip")
    files = unzip(response)
    folders = {name.split("/", 1)[0] for name in files}
    inner = {name.split("/", 1)[1] for name in files}

    assert response.headers["content-type"] == "application/zip"
    assert len(folders) == 1
    assert {"README.md", "data.json", "manifest.json"} <= inner
    assert len([name for name in inner if name.startswith("experiences/")]) == 2

    data = json.loads(by_suffix(files, "/data.json"))
    manifest = json.loads(by_suffix(files, "/manifest.json"))
    readme = by_suffix(files, "/README.md").decode("utf-8")

    assert len(data["experiences"]) == 2
    assert data["recycle_bin"] == []
    assert manifest["counts"] == {
        "experiences": 2, "profile": 0, "preferences": 0, "recycle_bin": 0, "attachments": 0,
    }

    folder = folders.pop()

    for entry in manifest["files"]:
        payload = files[f"{folder}/{entry['path']}"]
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
        assert len(payload) == entry["bytes"]

    assert "分享前请注意" in readme
    assert "不包含什么" in readme
    assert "附件" in readme


def test_the_recycle_bin_is_exported_separately_with_its_deadline(browser_a, clock):
    browser_a.create(title="还在用的经历", actions="a")
    binned = browser_a.create(title="回收站里的经历", actions="b")
    delete(browser_a, binned)
    files = unzip(browser_a.get("/export/all.zip"))
    data = json.loads(by_suffix(files, "/data.json"))
    binned_md = [
        payload.decode("utf-8") for name, payload in files.items() if "/recycle-bin/" in name
    ]

    assert [record["content"]["title"] for record in data["experiences"]] == ["还在用的经历"]
    assert [record["content"]["title"] for record in data["recycle_bin"]] == ["回收站里的经历"]
    assert data["recycle_bin"][0]["restore_deadline"] == "2026-11-05T16:00:00Z"
    assert len(binned_md) == 1
    assert "之前可以恢复" in binned_md[0]


def test_expired_or_permanently_deleted_content_is_never_exported(browser_a, clock):
    expired = browser_a.create(title="到期哨兵-91c2", actions="x")
    purged = browser_a.create(title="永久删除哨兵-5d7e", actions="y")
    delete(browser_a, expired)
    delete(browser_a, purged)
    response = browser_a.submit(
        f"/experiences/{purged}/purge",
        f"/experiences/{purged}/purge",
        acknowledge="yes",
    )
    assert response.status_code == 303
    clock.advance(days=7)
    # The 12-hour session has expired by now; sign in again.
    browser_a.sign_in("合成用户 A")
    files = unzip(browser_a.get("/export/all.zip"))
    everything = b"".join(files.values()).decode("utf-8")

    assert "到期哨兵-91c2" not in everything
    assert "永久删除哨兵-5d7e" not in everything
    assert json.loads(by_suffix(files, "/data.json"))["recycle_bin"] == []


def test_another_users_export_never_contains_my_content(browser_a, browser_b):
    item_id = browser_a.create(title="A 的项目", actions="A 的哨兵-3b8a", private_note="A 的备注哨兵-77e1")
    files = unzip(browser_b.get("/export/all.zip"))
    everything = b"".join(files.values()).decode("utf-8")

    assert "A 的哨兵-3b8a" not in everything
    assert "A 的备注哨兵-77e1" not in everything
    assert json.loads(by_suffix(files, "/data.json"))["experiences"] == []
    assert browser_b.get(f"/experiences/{item_id}/export.md").status_code == 404


def test_exported_files_open_outside_the_product(browser_a, tmp_path):
    browser_a.create(title="中文标题", actions="中文内容")

    with zipfile.ZipFile(io.BytesIO(browser_a.get("/export/all.zip").content)) as archive:
        archive.extractall(tmp_path / "unzipped")

    extracted = [path for path in (tmp_path / "unzipped").rglob("*") if path.is_file()]

    assert len(extracted) == 4

    for path in extracted:
        assert os.access(path, os.R_OK)
        path.read_text(encoding="utf-8")


def test_a_deleted_history_version_is_exported_with_its_own_deadline(browser_a):
    item_id = browser_a.create(actions="第一版")
    confirm = browser_a.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm")
    assert confirm.status_code == 303
    browser_a.submit(f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft", actions="第二版")
    history = browser_a.get(f"/experiences/{item_id}/history").text
    initial_link = [
        line for line in history.split('"') if line.startswith(f"/experiences/{item_id}/versions/") and line.endswith("/delete")
    ][-1]
    page = browser_a.get(initial_link).text
    response = browser_a.post(initial_link, form_for(page, initial_link).fields)
    assert response.status_code == 303
    text = browser_a.get(f"/experiences/{item_id}/export.md").content.decode("utf-8")

    assert "这个历史版本已单独删除" in text
    assert "## 历史版本（2 个）" in text
