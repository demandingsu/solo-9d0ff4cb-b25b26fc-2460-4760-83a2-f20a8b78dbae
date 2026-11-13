"""共享测试设施：静态文件服务器（可注入故障）、独立数据库的 API 客户端、
以及可原地改写的双目录示例站（a/ 与 b/ 两个入口目录，用于验证目录级切换）。
"""
from __future__ import annotations

import functools
import http.server
import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo_site"

# 加载示例数据生成器（脚本名带连字符，用 importlib）
spec = importlib.util.spec_from_file_location(
    "generate_demo_site", ROOT / "scripts" / "generate_demo_site.py"
)
gen = importlib.util.module_from_spec(spec)
sys.modules["generate_demo_site"] = gen
spec.loader.exec_module(gen)


@pytest.fixture(scope="session")
def gen_module():
    """示例站生成器模块（数据与变体定义）。"""
    return gen


class FlakyHandler(http.server.SimpleHTTPRequestHandler):
    # 类属性：需要返回 404 的路径集合
    fail_paths: set[str] = set()

    def do_GET(self):  # noqa: N802
        if self.path in self.fail_paths:
            self.send_error(404, "injected failure")
            return
        return super().do_GET()

    def log_message(self, *args):  # 静默
        pass


def _serve(directory: Path) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(FlakyHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


@pytest.fixture(scope="session")
def demo_server():
    """只读托管仓库内置示例站（demo_site/，v1 数据）。"""
    httpd = _serve(DEMO_DIR)
    host, port = httpd.server_address
    yield f"http://{host}:{port}", httpd, FlakyHandler
    httpd.shutdown()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 每个测试使用独立临时数据库
    monkeypatch.setenv("TENDER_DB_PATH", str(tmp_path / "test.db"))
    from app import config, database, indexing, merge, models, scraper, service

    importlib.reload(config)
    importlib.reload(indexing)
    importlib.reload(database)
    importlib.reload(scraper)
    importlib.reload(merge)
    importlib.reload(service)
    importlib.reload(models)
    import app.main as main

    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c, main, database


@pytest.fixture()
def mutable_site(tmp_path):
    """可改写的示例站：a/ 与 b/ 两个入口目录（同一服务器、同一来源）。

    regenerate(variant, catalog) 可原地重写某个目录的页面（如切到 v2 或
    故障样例），用于验证"复采某目录"时的原子切换与失败保留。
    """
    site_root = tmp_path / "site"
    (site_root / "a").mkdir(parents=True)
    (site_root / "b").mkdir(parents=True)
    gen.write_site(site_root / "a", "v1")
    gen.write_site(site_root / "b", "v1")

    httpd = _serve(site_root)
    host, port = httpd.server_address
    base = f"http://{host}:{port}"

    def regenerate(variant: str, catalog: str = "a") -> None:
        gen.write_site(site_root / catalog, variant)

    yield SimpleNamespace(
        base=base,
        root=site_root,
        regenerate=regenerate,
        list_url=lambda catalog="a": f"{base}/{catalog}/list_1.html",
        handler=FlakyHandler,
    )
    httpd.shutdown()
