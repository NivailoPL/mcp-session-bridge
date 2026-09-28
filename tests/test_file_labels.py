"""Admin-only file labels, admin file previews and image thumbnails."""
from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image
from starlette.testclient import TestClient

from tests.image_samples import make_image


def _setup(load_main):
    main = load_main(graph_experimental=True)
    main.store.create_session_group("Brainstorming", "#22c55e", "ideas")
    main.store.create_session_group("Other", "#ef4444", "camera")
    main.store.create_session("s1", "Files", group_id="brainstorming")
    main.store.create_session("s2", "Elsewhere", group_id="other")
    return main


def test_labels_are_created_per_group_and_assigned_to_visible_files(admin_client, load_main) -> None:
    main = _setup(load_main)
    session_file = main.store.save_session_file("s1", "notes.md", "# Notes")
    group_file = main.store.save_group_file_for_session("s1", "brief.md", "Brief")
    foreign = main.store.save_session_file("s2", "private.md", "Private")
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}

    assert client.post(
        "/admin/api/sessions/s1/file-labels", json={"name": "Ideas", "color": "yellow"}
    ).status_code == 403
    created = client.post(
        "/admin/api/sessions/s1/file-labels",
        json={"name": "  Research  ", "color": "purple"},
        headers=headers,
    )
    assert created.status_code == 200
    label = created.json()["label"]
    assert label["name"] == "Research"
    assert label["group_id"] == "brainstorming"

    duplicate = client.post(
        "/admin/api/sessions/s1/file-labels",
        json={"name": "research", "color": "pink"},
        headers=headers,
    )
    assert duplicate.status_code == 400
    assert client.post(
        "/admin/api/sessions/s1/file-labels",
        json={"name": "Bad", "color": "#ff0000"},
        headers=headers,
    ).status_code == 400

    assign_path = f"/admin/api/sessions/s1/file-labels/{label['label_id']}/files"
    assigned = client.post(
        assign_path,
        json={"file_ids": [session_file.file_id, group_file.file_id], "assigned": True},
        headers=headers,
    )
    assert assigned.status_code == 200
    assert {item["file_id"]: item["label_ids"] for item in assigned.json()["files"]} == {
        session_file.file_id: [label["label_id"]],
        group_file.file_id: [label["label_id"]],
    }

    # A file outside the session and its group cannot be labeled from here.
    assert client.post(
        assign_path,
        json={"file_ids": [foreign.file_id], "assigned": True},
        headers=headers,
    ).status_code == 409
    # Another group's session cannot see or use this group's label.
    assert client.post(
        f"/admin/api/sessions/s2/file-labels/{label['label_id']}/files",
        json={"file_ids": [foreign.file_id], "assigned": True},
        headers=headers,
    ).status_code == 404

    detail = client.get("/admin/api/sessions/s1").json()
    assert [item["name"] for item in detail["file_labels"]] == ["Research"]
    listed = {item["file_id"]: item for item in detail["files"]["session"] + detail["files"]["group"]}
    assert listed[session_file.file_id]["label_ids"] == [label["label_id"]]
    assert listed[session_file.file_id]["preview"] == "# Notes"
    assert client.get("/admin/api/sessions/s2").json()["file_labels"] == []

    removed = client.post(
        assign_path,
        json={"file_ids": [group_file.file_id], "assigned": False},
        headers=headers,
    )
    assert removed.json()["files"] == [{"file_id": group_file.file_id, "label_ids": []}]


def test_models_never_see_labels_or_previews(admin_client, load_main) -> None:
    main = _setup(load_main)
    saved = main.store.save_session_file("s1", "notes.md", "Secret sorting")
    label = main.store.create_file_label("brainstorming", "Important", "pink")
    main.store.set_file_label_assignment(label["label_id"], [saved.file_id], assigned=True, session_id="s1")

    manifests = main.store.list_session_files_for_session("s1")
    assert manifests and all("label_ids" not in item and "preview" not in item for item in manifests)


def test_moving_a_file_or_session_drops_its_labels(load_main) -> None:
    main = _setup(load_main)
    store = main.store
    session_file = store.save_session_file("s1", "notes.md", "Notes")
    kept = store.save_group_file_for_session("s1", "brief.md", "Brief")
    label = store.create_file_label("brainstorming", "Ideas", "yellow")
    store.set_file_label_assignment(
        label["label_id"], [session_file.file_id, kept.file_id], assigned=True, session_id="s1"
    )

    store.move_session_file(
        session_file.file_id,
        scope_type="group",
        group_id="brainstorming",
        visible_session_id="s1",
        visible_group_id="brainstorming",
    )
    labels = {item["file_id"]: item["label_ids"] for item in store.list_admin_session_files(
        session_id="s1", group_id="brainstorming"
    )}
    assert labels == {session_file.file_id: [], kept.file_id: [label["label_id"]]}

    other = store.save_session_file("s1", "draft.md", "Draft")
    store.set_file_label_assignment(label["label_id"], [other.file_id], assigned=True, session_id="s1")
    store.set_session_group("s1", "other")
    moved = store.list_admin_session_files(session_id="s1", group_id="other")
    assert {item["file_id"]: item["label_ids"] for item in moved if item["scope_type"] == "session"} == {
        other.file_id: []
    }
    # The group file stayed behind with its label.
    assert store.list_admin_session_files(session_id="", group_id="brainstorming")[0]["label_ids"] == [
        label["label_id"]
    ]


def test_deleting_a_label_or_group_keeps_the_files(admin_client, load_main) -> None:
    main = _setup(load_main)
    saved = main.store.save_group_file_for_session("s1", "brief.md", "Brief")
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}
    label = client.post(
        "/admin/api/sessions/s1/file-labels", json={"name": "Ideas", "color": "lime"}, headers=headers
    ).json()["label"]
    path = f"/admin/api/sessions/s1/file-labels/{label['label_id']}"

    renamed = client.patch(path, json={"name": "Big ideas", "color": "orange"}, headers=headers)
    assert renamed.json()["label"] | {"updated_at": 0} == label | {
        "name": "Big ideas", "color": "orange", "updated_at": 0,
    }
    assert client.patch(path, json={"group_id": "other"}, headers=headers).status_code == 400

    client.post(path + "/files", json={"file_ids": [saved.file_id], "assigned": True}, headers=headers)
    assert client.delete(path, headers=headers).status_code == 200
    assert client.delete(path, headers=headers).status_code == 404
    assert main.store.get_session_file(saved.file_id) is not None
    assert main.store.list_admin_session_files(session_id="s1", group_id="brainstorming")[0]["label_ids"] == []

    main.store.create_file_label("brainstorming", "Research", "sky")
    main.store.delete_session_group("brainstorming", "other")
    assert main.store.list_file_labels("brainstorming") == []
    assert main.store.get_session_file(saved.file_id).group_id == "other"


def test_image_thumbnails_are_small_jpegs_for_admins_only(admin_client, load_main) -> None:
    main = _setup(load_main)
    client, csrf = admin_client(main)
    uploaded = client.post(
        "/admin/api/sessions/s1/files",
        json={
            "scope_type": "session",
            "filename": "wide.png",
            "content_base64": base64.b64encode(make_image("PNG", size=(1600, 900))).decode(),
        },
        headers={"x-csrf-token": csrf},
    ).json()["file"]

    thumbnail = client.get(f"/admin/api/files/{uploaded['file_id']}/thumbnail")
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"] == "image/jpeg"
    with Image.open(BytesIO(thumbnail.content)) as image:
        assert max(image.size) == 480

    text_file = main.store.save_session_file("s1", "notes.md", "Text")
    assert client.get(f"/admin/api/files/{text_file.file_id}/thumbnail").status_code == 400
    assert client.get("/admin/api/files/999999/thumbnail").status_code == 404

    anonymous = TestClient(main.app, base_url="http://127.0.0.1:8787")
    assert anonymous.get(f"/admin/api/files/{uploaded['file_id']}/thumbnail").status_code == 401
