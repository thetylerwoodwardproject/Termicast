import subprocess
from unittest.mock import Mock

import pytest

from termicast import s3deploy
from termicast.models import new_show
from termicast.s3deploy import object_key, s4cmd_args, upload_file, deploy_paths


def s3_show(tmp_path):
    output_dir = str(tmp_path / "out") if tmp_path else "/tmp/termicast-s3-test"
    return new_show(
        title="S3 show", description="d", base_url="https://cdn.example.org/show",
        output_dir=output_dir, hosting="s3", endpoint_url="https://s3.example.org",
        bucket="my-bucket", prefix="my-show", enabled=True,
    )


def test_object_key_with_and_without_prefix():
    show = s3_show(None)
    assert object_key(show, "audio/x.mp3") == "my-show/audio/x.mp3"
    show["prefix"] = ""
    assert object_key(show, "feed.xml") == "feed.xml"


def test_s4cmd_args_include_controls_and_content_type():
    show = s3_show(None)
    args = s4cmd_args(show, "audio/mpeg")
    assert args[1] == "put"
    assert "s4cmd" in args[0]
    assert "--endpoint-url" in args and "https://s3.example.org" in args
    assert "--num-threads" in args and "2" in args
    assert "--multipart-split-size" in args and str(16 * 1024 * 1024) in args
    assert "--max-singlepart-upload-size" in args and str(64 * 1024 * 1024) in args
    assert "--force" in args and "--sync-check" in args
    assert "--API-ContentType" in args and "audio/mpeg" in args


def test_s4cmd_args_omit_endpoint_for_aws():
    show = s3_show(None)
    show["endpoint_url"] = ""
    assert "--endpoint-url" not in s4cmd_args(show, "audio/mpeg")


def test_upload_file_runs_s4cmd(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    (tmp_path / "out" / "audio").mkdir(parents=True)
    local = tmp_path / "out" / "audio" / "x.mp3"
    local.write_bytes(b"data")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(s3deploy, "subprocess", Mock(run=run, DEVNULL=subprocess.DEVNULL))
    upload_file(show, local, "audio/x.mp3")
    args = run.call_args.args[0]
    assert args[-1] == "s3://my-bucket/my-show/audio/x.mp3"


def test_upload_file_rejects_unknown_content_type(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    (tmp_path / "out").mkdir(parents=True)
    local = tmp_path / "out" / "weird.bin"
    local.write_bytes(b"data")
    with pytest.raises(ValueError, match="No Content-Type mapping"):
        upload_file(show, local, "weird.bin")


def test_deploy_paths_uploads_in_order(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    root = tmp_path / "out"
    (root / "audio").mkdir(parents=True)
    for relative in ("audio/x.mp3", "chapters/x.json"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
    uploaded = []
    monkeypatch.setattr(s3deploy, "upload_file", lambda show, local, rel, dry_run=False: uploaded.append(rel))
    deploy_paths(show, ["audio/x.mp3", "chapters/x.json"], source_root=root, verify=False)
    assert uploaded == ["audio/x.mp3", "chapters/x.json"]


def test_deploy_paths_stops_on_failure(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    root = tmp_path / "out"
    for relative in ("audio/x.mp3", "chapters/x.json"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")

    def fail(show, local, rel, dry_run=False):
        if rel == "audio/x.mp3":
            raise RuntimeError("upload failed")
        return None

    monkeypatch.setattr(s3deploy, "upload_file", fail)
    with pytest.raises(RuntimeError, match="audio/x.mp3"):
        deploy_paths(show, ["audio/x.mp3", "chapters/x.json"], source_root=root, verify=False)
