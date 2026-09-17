import asyncio
import base64
import hashlib
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest

from tests.image_samples import make_image
from app import image_files
from app.image_worker import inspect_image
from app.storage import Store, SessionFileConflictError, ImageStorageQuotaError


def upload_admin_image(admin_client, main, session_id, filename, raw):
    client, csrf = admin_client(main)
    return client.post(f"/admin/api/sessions/{session_id}/files",
        headers={"x-csrf-token": csrf},
        json={"scope_type": "session", "filename": filename,
              "content_base64": base64.b64encode(raw).decode()})


def test_image_upload_persists_original_and_returns_native_image(load_main, admin_client):
    main = load_main()
    main.store.create_session("images", "Images")
    raw = make_image()
    response = upload_admin_image(admin_client, main, "images", "test.png", raw)
    assert response.status_code == 200, response.text
    file = response.json()["file"]
    assert file["content_kind"] == "image"
    assert file["text_available"] is False
    assert file["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "data" not in file and "content" not in file
    viewed = asyncio.run(main.view_session_image("images", file["file_id"]))
    assert not viewed.isError
    assert base64.b64decode(viewed.content[1].data) == raw


@pytest.mark.parametrize("raw,name", [(b"not an image", "x.png"), (make_image(), "x.jpg"), (make_image("GIF"), "x.gif")])
def test_invalid_upload_does_not_write(load_main, admin_client, raw, name):
    main = load_main()
    main.store.create_session("images", "Images")
    response = upload_admin_image(admin_client, main, "images", name, raw)
    assert response.status_code == 400
    assert main.list_session_files("images")["files"] == []


def test_admin_image_upload_and_raw_download(admin_client, load_main):
    main = load_main()
    main.store.create_session("images", "Images")
    client, csrf = admin_client(main)
    raw = make_image("JPEG")
    response = client.post("/admin/api/sessions/images/files", headers={"x-csrf-token": csrf},
        json={"scope_type": "session", "filename": "test.jpg", "content_base64": base64.b64encode(raw).decode()})
    assert response.status_code == 200, response.text
    file = response.json()["file"]
    response = client.get(f"/admin/api/files/{file['file_id']}/raw?download=1")
    assert response.content == raw
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment")


def test_export_includes_original_image(load_main, admin_client, tmp_path):
    from pathlib import Path
    from app.conversation_export import export_database_to_markdown

    main = load_main()
    main.store.create_session("images", "Images")
    raw = make_image()
    response = upload_admin_image(admin_client, main, "images", "original.png", raw)
    assert response.status_code == 200, response.text
    exported = export_database_to_markdown(main.store.db_path, export_root=tmp_path / "exports")
    files = list(Path(exported["artifact"]["path"]).rglob("*.png"))
    assert len(files) == 1 and files[0].read_bytes() == raw


def test_scope_move_delete_and_reopen(load_main):
    main = load_main()
    store = main.store
    group = store.create_session_group("Images", "#123456", "camera", group_id="images")
    for session_id, group_id in [("a", "images"), ("b", "images"), ("c", "uncategorized")]:
        store.create_session(session_id, session_id, group_id=group_id)
    raw = make_image()
    image = image_files.validate_image_isolated("test.png", raw)
    saved = store.save_image("test.png", image, session_id="a")
    assert store.get_image_for_session(" a ", saved.file_id)[1] == raw
    with pytest.raises(SessionFileConflictError):
        store.get_image_for_session("b", saved.file_id)
    store.move_session_file(saved.file_id, scope_type="group", group_id=group.group_id)
    assert store.get_image_for_session("b", saved.file_id)[1] == raw
    with pytest.raises(SessionFileConflictError):
        store.get_image_for_session("c", saved.file_id)
    reopened = Store(store.db_path, allow_startup_migrations=False)
    assert reopened.get_image_for_session("a", saved.file_id)[1] == raw
    manifest = main.list_session_files("b")["files"][0]
    assert manifest["text_available"] is False
    assert "binary_content" not in manifest and "data" not in manifest
    downloaded = main.download_session_file("b", saved.file_id)
    assert downloaded["view_tool"] == "view_session_image"
    assert "content" not in downloaded["file"]
    with pytest.raises(ValueError, match="cannot be edited"):
        store.update_session_file(saved.file_id, "replacement", expected_sha256=saved.sha256)
    assert store.get_image_for_session("a", saved.file_id)[1] == raw
    store.set_session_group("b", "uncategorized")
    with pytest.raises(SessionFileConflictError):
        store.get_image_for_session("b", saved.file_id)
    store.set_session_group("c", group.group_id)
    assert store.get_image_for_session("c", saved.file_id)[1] == raw
    store.delete_session_file(saved.file_id)
    assert store.get_image_for_session("a", saved.file_id) is None
    assert asyncio.run(main.view_session_image("a", saved.file_id)).isError


def test_image_quota_is_atomic_across_stores_and_recovers_after_delete(tmp_path):
    raw = make_image()
    image = image_files.validate_image_isolated("test.png", raw)
    first = Store(tmp_path / "bridge.sqlite3", image_storage_max_bytes=len(raw))
    first.create_session("a", "A")
    second = Store(first.db_path, image_storage_max_bytes=len(raw))
    def save(store):
        try:
            return store.save_image("test.png", image, session_id="a")
        except ImageStorageQuotaError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, [first, second]))
    saved = [result for result in results if result is not None]
    assert len(saved) == 1
    first.delete_session_file(saved[0].file_id)
    assert save(second) is not None


@pytest.mark.parametrize("encoded", ["", "not base64!", "é", "a" * (((image_files.MAX_IMAGE_BYTES + 2) // 3) * 4 + 1)])
def test_invalid_base64_and_encoded_limit(encoded):
    with pytest.raises(ValueError):
        image_files.decode_image_base64(encoded)


def test_decoded_size_boundary(monkeypatch):
    monkeypatch.setattr(image_files, "MAX_IMAGE_BYTES", 3)
    assert image_files.decode_image_base64("YWJj") == b"abc"
    with pytest.raises(ValueError):
        image_files.decode_image_base64("YWJjZA==")


def test_pixel_boundary_animation_and_truncation(monkeypatch):
    from PIL import Image
    from app import image_worker
    monkeypatch.setattr(image_worker, "MAX_IMAGE_PIXELS", 192)
    assert inspect_image(make_image(size=(16, 12))) == "image/png"
    with pytest.raises(ValueError, match="megapixels"):
        inspect_image(make_image(size=(17, 12)))
    output = BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="PNG", save_all=True,
        append_images=[Image.new("RGB", (8, 8), "blue")], duration=100)
    with pytest.raises(ValueError, match="Animated"):
        inspect_image(output.getvalue())
    with pytest.raises(ValueError):
        inspect_image(make_image("JPEG")[:-15])


def test_worker_timeout_and_invalid_result(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("image-worker", 1)
    monkeypatch.setattr(image_files.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="timed out"):
        image_files.validate_image_isolated("x.png", make_image())
    monkeypatch.setattr(image_files.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess("worker", -9, stdout=b""))
    with pytest.raises(ValueError, match="safety limits"):
        image_files.validate_image_isolated("x.png", make_image())


def test_worker_admission_and_cancellation_are_bounded():
    release = threading.Event()
    def work():
        release.wait(timeout=5)
    async def scenario():
        tasks = [asyncio.create_task(image_files.run_image_worker(work)) for _ in range(4)]
        for _ in range(100):
            if image_files._image_worker_admitted == 4:
                break
            await asyncio.sleep(0.01)
        try:
            with pytest.raises(image_files.ImageWorkerBusyError):
                await image_files.run_image_worker(work)
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert image_files._image_worker_admitted == 4
        finally:
            release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for _ in range(100):
            if image_files._image_worker_admitted == 0:
                break
            await asyncio.sleep(0.01)
        assert image_files._image_worker_admitted == 0
    asyncio.run(scenario())


def test_admin_group_upload_auth_csrf_and_edit_block(admin_client, load_main):
    from starlette.testclient import TestClient
    main = load_main()
    main.store.create_session("a", "A")
    payload = {"scope_type": "group", "filename": "x.png", "content_base64": base64.b64encode(make_image()).decode()}
    anonymous = TestClient(main.app, base_url="http://127.0.0.1:8787")
    assert anonymous.post("/admin/api/sessions/a/files", json=payload).status_code == 401
    client, csrf = admin_client(main)
    assert client.post("/admin/api/sessions/a/files", json=payload).status_code == 403
    response = client.post("/admin/api/sessions/a/files", json=payload, headers={"x-csrf-token": csrf})
    assert response.status_code == 200, response.text
    file = response.json()["file"]
    assert file["scope_type"] == "group" and file["group_id"] == "uncategorized"
    assert anonymous.get(f"/admin/api/files/{file['file_id']}/raw").status_code == 401
    response = client.patch(f"/admin/api/sessions/a/files/{file['file_id']}",
        json={"content": "overwrite", "expected_sha256": file["sha256"]}, headers={"x-csrf-token": csrf})
    assert response.status_code == 400


def test_image_quota_config_and_errors(load_main, admin_client):
    main = load_main(env={"BRIDGE_IMAGE_STORAGE_MAX_BYTES": "1"})
    main.store.create_session("a", "A")
    assert main.settings.image_storage_max_bytes == 1
    response = upload_admin_image(admin_client, main, "a", "x.png", make_image())
    assert response.status_code == 507
    assert main.list_session_files("a")["files"] == []


def test_upload_busy_is_retryable(load_main, admin_client, monkeypatch):
    main = load_main()
    monkeypatch.setattr(image_files, "_image_worker_admitted", 4)
    main.store.create_session("a", "A")
    response = upload_admin_image(admin_client, main, "a", "x.png", make_image())
    assert response.status_code == 503
    assert response.headers["retry-after"] == "2"
    assert main.list_session_files("a")["files"] == []
