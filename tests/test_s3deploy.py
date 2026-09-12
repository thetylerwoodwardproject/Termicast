import subprocess
from unittest.mock import Mock

import pytest

from termicast import s3deploy
from termicast.models import new_show
from termicast.s3deploy import (
    object_key, s4cmd_args, upload_file, deploy_paths, remote_rename, check_s3_destination,
)


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


def test_upload_file_access_denied_includes_iam_policy_for_aws(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    show["endpoint_url"] = ""
    (tmp_path / "out" / "audio").mkdir(parents=True)
    local = tmp_path / "out" / "audio" / "x.mp3"
    local.write_bytes(b"data")
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "AccessDenied on PutObject"))
    monkeypatch.setattr(s3deploy, "subprocess", Mock(run=run, DEVNULL=subprocess.DEVNULL))
    with pytest.raises(RuntimeError, match='"s3:PutObject"'):
        upload_file(show, local, "audio/x.mp3")


def test_upload_file_access_denied_gives_generic_help_for_s3_compatible(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    (tmp_path / "out" / "audio").mkdir(parents=True)
    local = tmp_path / "out" / "audio" / "x.mp3"
    local.write_bytes(b"data")
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "AccessDenied on PutObject"))
    monkeypatch.setattr(s3deploy, "subprocess", Mock(run=run, DEVNULL=subprocess.DEVNULL))
    with pytest.raises(RuntimeError, match="storage provider's access key"):
        upload_file(show, local, "audio/x.mp3")


def test_upload_file_non_permission_error_has_no_extra_help(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    (tmp_path / "out" / "audio").mkdir(parents=True)
    local = tmp_path / "out" / "audio" / "x.mp3"
    local.write_bytes(b"data")
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "no such bucket"))
    monkeypatch.setattr(s3deploy, "subprocess", Mock(run=run, DEVNULL=subprocess.DEVNULL))
    with pytest.raises(RuntimeError) as excinfo:
        upload_file(show, local, "audio/x.mp3")
    assert "storage provider" not in str(excinfo.value)
    assert "IAM" not in str(excinfo.value)


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


def test_remote_rename_runs_s4cmd_mv(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(s3deploy, "subprocess", Mock(run=run, DEVNULL=subprocess.DEVNULL))
    remote_rename(show, "audio/old.mp3", "audio/new.mp3")
    args = run.call_args.args[0]
    assert args[1] == "mv"
    assert "--endpoint-url" in args and "https://s3.example.org" in args
    assert args[-2] == "s3://my-bucket/my-show/audio/old.mp3"
    assert args[-1] == "s3://my-bucket/my-show/audio/new.mp3"


def test_remote_rename_raises_on_failure(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "no such key"))
    monkeypatch.setattr(s3deploy, "subprocess", Mock(run=run, DEVNULL=subprocess.DEVNULL))
    with pytest.raises(RuntimeError, match="no such key"):
        remote_rename(show, "audio/old.mp3", "audio/new.mp3")


def test_check_s3_destination_passes_when_empty_and_writable(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    ok = subprocess.CompletedProcess([], 0, "", "")
    run = Mock(side_effect=[ok, ok, ok])
    monkeypatch.setattr(s3deploy, "subprocess", Mock(run=run, DEVNULL=subprocess.DEVNULL))
    check_s3_destination(show)
    assert run.call_count == 3
    assert run.call_args_list[0].args[0][1] == "ls"
    assert run.call_args_list[1].args[0][1] == "put"
    assert run.call_args_list[2].args[0][1] == "del"


def test_check_s3_destination_rejects_nonempty_destination(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    nonempty = subprocess.CompletedProcess([], 0, "some/key\n", "")
    run = Mock(return_value=nonempty)
    monkeypatch.setattr(s3deploy, "subprocess", Mock(run=run, DEVNULL=subprocess.DEVNULL))
    with pytest.raises(ValueError, match="already contains objects"):
        check_s3_destination(show)
    assert run.call_count == 1


def test_check_s3_destination_fails_when_write_denied(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    empty = subprocess.CompletedProcess([], 0, "", "")
    denied = subprocess.CompletedProcess([], 1, "", "AccessDenied on PutObject")
    run = Mock(side_effect=[empty, denied])
    monkeypatch.setattr(s3deploy, "subprocess", Mock(run=run, DEVNULL=subprocess.DEVNULL))
    with pytest.raises(RuntimeError, match="listable but not writable.*AccessDenied"):
        check_s3_destination(show)


def test_check_s3_destination_warns_when_probe_cleanup_fails(tmp_path, monkeypatch):
    show = s3_show(tmp_path)
    empty = subprocess.CompletedProcess([], 0, "", "")
    put_ok = subprocess.CompletedProcess([], 0, "", "")
    del_failed = subprocess.CompletedProcess([], 1, "", "no delete permission")
    run = Mock(side_effect=[empty, put_ok, del_failed])
    monkeypatch.setattr(s3deploy, "subprocess", Mock(run=run, DEVNULL=subprocess.DEVNULL))
    with pytest.raises(RuntimeError, match="could not remove it"):
        check_s3_destination(show)


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
