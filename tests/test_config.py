import sys
import types

from knesset.config import Settings, detect_device


def test_scope_and_dsn():
    s = Settings(scope_knessets="25, 26", etl_password="pw", db_host="h", db_port=5)
    assert s.knessets == [25, 26]
    assert s.dsn() == "postgresql://etl:pw@h:5/knesset"


def test_device_explicit():
    assert detect_device("cpu") == "cpu"


def fake_torch(mps: bool, cuda: bool) -> types.ModuleType:
    torch = types.ModuleType("torch")
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps))
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    return torch


def test_device_auto(monkeypatch):
    for mps, cuda, expected in [(True, True, "mps"), (False, True, "cuda"), (False, False, "cpu")]:
        monkeypatch.setitem(sys.modules, "torch", fake_torch(mps, cuda))
        assert detect_device() == expected


def test_device_without_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)  # makes `import torch` raise ImportError
    assert detect_device() == "cpu"


def test_env_file_is_the_repos_wherever_we_run(tmp_path, monkeypatch):
    """Notebooks run in notebooks/: settings must still read the repo's .env."""
    from knesset.config import ROOT, Settings

    monkeypatch.chdir(tmp_path)
    assert Settings.model_config["env_file"][0] == ROOT / ".env"
    assert (ROOT / "pyproject.toml").exists()
