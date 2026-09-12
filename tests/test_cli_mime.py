from unittest.mock import Mock

import pytest

from termicast import cli, prompts


@pytest.mark.parametrize("interactive,accept", [(True, True), (True, False), (False, False)])
def test_deploy_mime_failure_offers_repair_only_in_terminal(monkeypatch, interactive, accept):
    db = Mock()
    db.get_show.return_value = {"id": "001"}
    publisher = Mock()
    publisher.deploy.side_effect = RuntimeError("Unexpected Content-Type 'text/xml' (expected application/rss+xml)")
    monkeypatch.setattr(cli, "Database", lambda: db)
    monkeypatch.setattr(cli, "Publisher", lambda db: publisher)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: interactive)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: interactive)
    confirm = Mock(return_value=accept)
    monkeypatch.setattr(cli, "confirm", confirm)
    repair = Mock(return_value=True)
    monkeypatch.setattr(prompts, "correct_host_mime", repair)
    assert cli.main(["deploy", "001"]) == 1
    assert repair.call_count == int(interactive and accept)
    assert confirm.call_count == int(interactive)
    publisher.deploy.assert_called_once()


@pytest.mark.parametrize("result,exit_code", [(True, 0), (None, 1)])
def test_direct_mime_repair_reports_verification_result(monkeypatch, result, exit_code):
    db = Mock()
    show = {"id": "001"}
    db.get_show.return_value = show
    monkeypatch.setattr(cli, "Database", lambda: db)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    repair = Mock(return_value=result)
    monkeypatch.setattr(prompts, "correct_host_mime", repair)
    assert cli.main(["fix-host-mime", "001"]) == exit_code
    repair.assert_called_once_with(db, show)
