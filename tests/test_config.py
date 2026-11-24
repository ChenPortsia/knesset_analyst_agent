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


def test_langfuse_exporter_only_with_keys(monkeypatch):
    import base64

    from knesset import tracing
    from knesset.config import settings

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")  # the repo's .env may hold real keys
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    settings.cache_clear()
    assert tracing.langfuse_exporter() is None  # no keys: nothing leaves the machine
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com/")
    settings.cache_clear()
    exporter = tracing.langfuse_exporter()
    assert exporter._endpoint == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    expected = base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    assert exporter._client._headers["authorization"] == f"Basic {expected}"


def test_langfuse_keeps_the_phoenix_exporter(monkeypatch):
    """phoenix.otel replaces its own exporter when another processor is added, by default."""
    from phoenix.otel import TracerProvider

    from knesset import tracing
    from knesset.config import settings

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    settings.cache_clear()
    provider = TracerProvider(endpoint="http://127.0.0.1:1/v1/traces", protocol="http/protobuf",
                              verbose=False)  # fmt: skip
    assert provider._default_processor is not None
    tracing.add_langfuse(provider)
    assert provider._default_processor is not None  # Phoenix still gets every span
    names = [type(p).__name__ for p in provider._active_span_processor._span_processors]
    assert len(names) == 2, names
    provider.shutdown()
