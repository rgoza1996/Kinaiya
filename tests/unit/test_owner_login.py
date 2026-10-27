import json
from types import SimpleNamespace

import pytest

from services.common.auth import load_or_create_tokens
from services.launcher import owner_login


def test_export_is_private_owner_only_and_never_prints_credential(tmp_path, capsys):
    tokens = load_or_create_tokens(tmp_path)
    output = tmp_path / "login.txt"
    owner_login(SimpleNamespace(data_dir=tmp_path, output=output))
    assert output.read_text() == tokens["owner"]
    assert output.stat().st_mode & 0o777 == 0o600
    report = capsys.readouterr().out
    assert json.loads(report)["scope"] == "owner-only browser sign-in"
    assert all(value not in report for value in tokens.values())
    with pytest.raises(FileExistsError):
        owner_login(SimpleNamespace(data_dir=tmp_path, output=output))
    assert output.read_text() == tokens["owner"]
