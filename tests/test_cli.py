import pytest

from termicast import cli


def test_help_lists_new_commands(data_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for command in ("add", "deploy", "doctor", "publish-due", "validate", "archive", "restage"):
        assert command in output
    assert not data_dir.exists()


def test_validate_empty(capsys):
    assert cli.main(["validate"]) == 0
    assert "No managed feeds to validate." in capsys.readouterr().out


def test_publish_due_empty(data_dir, capsys):
    assert cli.main(["publish-due"]) == 0
    assert "Published 0 due episode(s)." in capsys.readouterr().out


def test_doctor_empty(capsys):
    assert cli.main(["doctor"]) == 0
    assert "No hosting problems found." in capsys.readouterr().out


def test_doctor_unknown_show(capsys):
    assert cli.main(["doctor", "missing"]) == 1
    assert "Unknown podcast ID" in capsys.readouterr().out


def test_deploy_unknown_show(capsys):
    assert cli.main(["deploy", "missing"]) == 1
    assert "Unknown podcast ID" in capsys.readouterr().out


def test_add_unknown_show(capsys):
    assert cli.main(["add", "missing", "audio.mp3"]) == 1
    assert "Unknown podcast ID" in capsys.readouterr().out
