"""Run existing memory boundary regressions against installed release ZIPs."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import types

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--test-deps", type=Path, help="optional directory containing pytest and pytest-asyncio")
    parser.add_argument("--child", choices=["livingmemory", "self_learning"])
    args = parser.parse_args()
    output = args.output.resolve()
    core = args.core.resolve()
    if not args.child:
        output.mkdir(parents=True, exist_ok=False)
        data = output / "installed/data"
        data.mkdir(parents=True)
        from install import install
        install(data, apply=True)
        result = []
        for name in ("livingmemory", "self_learning"):
            with (output / f"{name}.log").open("w", encoding="utf-8") as log:
                command = [sys.executable, "-B", __file__, "--core", str(core),
                           "--output", str(output), "--child", name]
                if args.test_deps:
                    command += ["--test-deps", str(args.test_deps.resolve())]
                run = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=300)
            result.append({"component": name, "exit_code": run.returncode})
        (output / "results.json").write_text(json.dumps(result, indent=2), "utf-8")
        print(json.dumps(result))
        return int(any(r["exit_code"] != 0 for r in result))
    os.environ["ASTRBOT_ROOT"] = str(output / f"runtime-{args.child}")
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    real_connect, real_connect_ex, real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    def check(host):
        if host not in (None, '127.0.0.1', '::1', 'localhost', b'localhost'):
            raise RuntimeError('External network disabled during regression tests')
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6): check(address[0])
        return real_connect(sock, address)
    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6): check(address[0])
        return real_connect_ex(sock, address)
    def getaddrinfo(host, *pos, **kw):
        check(host)
        return real_getaddrinfo(host, *pos, **kw)
    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, getaddrinfo
    plugins = output / "installed/data/plugins"
    plugin = plugins / 'astrbot_plugin_livingmemory'
    source = ROOT / 'plugins/astrbot_plugin_livingmemory'
    if args.child == 'self_learning':
        plugin, source = plugin / 'learning', source / 'learning'
    sys.path[:0] = [str(core), str(plugins), str(plugin)]
    if args.test_deps:
        sys.path.insert(0, str(args.test_deps.resolve()))
    os.chdir(plugin)
    import astrbot.api  # Require the real core; do not silently use test stubs.
    package = types.ModuleType('astrbot_plugin_livingmemory')
    package.__path__ = [str(plugins / 'astrbot_plugin_livingmemory')]
    sys.modules['astrbot_plugin_livingmemory'] = package
    if args.child == "self_learning":
        spec = importlib.util.spec_from_file_location("self_learning_EterU", plugin / "__init__.py",
                                                    submodule_search_locations=[str(plugin)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cases = ["tests/unit/test_memory_authority.py", "tests/unit/test_source_lifecycle.py",
                 "tests/unit/test_feature_delegation.py", "tests/unit/test_learning_chain_regressions.py",
                 "tests/unit/test_learning_quality_monitor.py", "tests/unit/test_tiered_learning_trigger.py",
                 "tests/unit/test_config.py", "tests/unit/test_config_service.py",
                 "tests/unit/test_framework_llm_adapter.py"]
    else:
        cases = [f"tests/{name}.py" for name in (
            "test_memory_v02_invariants", "test_memory_v03_audit", "test_memory_v03_completion",
            "test_memory_v04_review", "test_memory_v05_review", "test_memory_scope", "test_injection_adapter",
            "test_llm_usage", "test_llm_streaming", "test_memory_processor", "test_memory_processor_regressions",
            "test_consolidation_manager", "test_features_77_76_74_59", "test_unified_dispatch")]
    cases = [str(source / case) for case in cases]
    import pytest
    return pytest.main(["-p", "pytest_asyncio.plugin", "-o", "addopts=", *cases, "-q", "--disable-warnings", "--tb=short",
                        f"--junitxml={output / (args.child + '.xml')}"])


if __name__ == "__main__":
    raise SystemExit(main())
