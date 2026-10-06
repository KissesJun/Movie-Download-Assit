"""Windows 启动辅助：配置检查、端口占用提示、迁移备份和日志。"""

import argparse
from contextlib import closing
from datetime import datetime
from importlib import metadata
import json
import logging
from logging.handlers import RotatingFileHandler
import ntpath
from pathlib import Path
import posixpath
import socket
import sqlite3
import sys
import threading
import time
import uuid
import webbrowser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_DIR = Path(__file__).resolve().parent


def install_config(root):
    target = root / "config.json"
    if target.exists():
        return False
    sample = (root / "config.example.json").read_bytes()
    try:
        with target.open("xb") as output:
            output.write(sample)
    except FileExistsError:
        return False
    return True


def check_dependencies(root):
    for line in (root / "requirements.txt").read_text(encoding="utf-8-sig").splitlines():
        requirement = line.split(";", 1)[0].strip()
        if not requirement or requirement.startswith("#"):
            continue
        name, version = requirement.split("==", 1)
        name = name.split("[", 1)[0]
        if name == "tzdata" and sys.platform != "win32":
            continue
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            raise ValueError(f"缺少依赖 {name}，请运行 setup.bat。") from None
        if installed != version:
            raise ValueError(f"依赖 {name} 版本与发布版本不符，请运行 setup.bat。")


def load_settings(root):
    try:
        settings = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ValueError("没有 config.json，请先运行 setup.bat，再填写配置。") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"config.json 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列。Windows 路径中的反斜杠需写成两个，或使用 /。") from None
    if not isinstance(settings, dict):
        raise ValueError("config.json 顶层必须是 JSON 对象。")
    for section in ("server", "qbittorrent", "download"):
        if not isinstance(settings.get(section), dict):
            raise ValueError(f"config.json 缺少 {section} 配置对象，请参考 config.example.json。")
    for section in ("server", "qbittorrent"):
        host, port = settings[section].get("host"), settings[section].get("port")
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"{section}.host 必须填写地址。")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{section}.port 必须是 1–65535 的整数。")
    qbt = settings["qbittorrent"]
    if not qbt.get("username") or not qbt.get("password") or qbt["password"] == "CHANGE_ME":
        raise ValueError("请先填写 config.json 中的 qbittorrent.username 和 password，并在 qBittorrent 中启用 WebUI。")
    download = settings["download"]
    complete = download.get("complete_root") or download.get("save_path")
    for name, value in (("complete_root / save_path", complete), ("temp_root", download.get("temp_root") or complete)):
        if not isinstance(value, str) or not (ntpath.isabs(value) or posixpath.isabs(value)):
            raise ValueError(f"download.{name} 必须是下载器机器上的绝对路径。")
    try:
        ZoneInfo(settings.get("display", {}).get("timezone", "Asia/Tokyo"))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("display.timezone 无效或缺少时区数据，请检查配置并运行 setup.bat。") from None
    return settings


def bind_listener(server):
    last_error = None
    for family, kind, protocol, _, address in socket.getaddrinfo(server["host"], server["port"], type=socket.SOCK_STREAM):
        listener = socket.socket(family, kind, protocol)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(address)
            listener.listen(128)
            listener.setblocking(False)
            return listener
        except OSError as exc:
            last_error = exc
            listener.close()
    raise ValueError(f"无法监听 {server['host']}:{server['port']}：端口可能已占用或地址不可用。请打开已运行的页面，或修改 server.port；不要重复启动。系统错误：{last_error}")


def check_database(root, backup=False):
    database = root / "data" / "app.db"
    if not database.exists():
        return None
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)) as source:
        version = source.execute("PRAGMA user_version").fetchone()[0]
        if version > 3:
            raise ValueError("数据库版本高于当前程序支持版本，请使用匹配的新版程序。")
        if version >= 3 or not backup:
            return None
        directory = root / "data" / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"app-v{version}-{datetime.now():%Y%m%d_%H%M%S}-{uuid.uuid4().hex[:8]}.db"
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("升级前备份校验失败，已停止启动；原数据库未修改。")
        return target


def browser_url(server):
    host = server["host"]
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1" if host == "0.0.0.0" else "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{server['port']}"


def open_when_ready(server, url):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not server.should_exit:
        if server.started:
            try:
                webbrowser.open(url)
            except Exception:
                logging.getLogger(__name__).warning("无法自动打开浏览器，请手动访问 %s", url)
            return
        time.sleep(.1)


def main(argv=None, root=BASE_DIR):
    parser = argparse.ArgumentParser(description="影视下载助手启动检查")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--setup", action="store_true", help="生成缺失的配置，不覆盖已有配置")
    group.add_argument("--check", action="store_true", help="检查配置、依赖和端口，不启动、不迁移数据库")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, "reconfigure"):
            output.reconfigure(encoding="utf-8", errors="replace")
    try:
        if sys.version_info < (3, 10):
            raise ValueError("需要 Python 3.10 或更新版本。")
        if args.setup:
            created = install_config(root)
            print("已生成 config.json。" if created else "保留现有 config.json，账号、路径和历史数据均未覆盖。")
            print("安装完成。下一步：\n1. 打开 config.json，填写 qBittorrent WebUI 地址、端口、用户名和密码。\n2. 检查 download 暂存、完成根目录，路径属于下载器所在机器。\n3. 保存配置，双击 run.bat。安装不会启动下载。\n详细说明：README.md。")
            return 0
        check_dependencies(root)
        settings = load_settings(root)
        with closing(bind_listener(settings["server"])) as listener:
            backup = check_database(root, backup=not args.check)
            url = browser_url(settings["server"])
            if args.check:
                print(f"本地检查通过。页面地址：{url}\n未启动服务、未升级数据库；下载器连接和搜索源需在页面内验证。")
                return 0
            if backup:
                print(f"数据库升级前备份：{backup}")
            logs = root / "logs"
            logs.mkdir(exist_ok=True)
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s", handlers=[logging.StreamHandler(), RotatingFileHandler(logs / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")])
            print(f"页面：{url}\n日志：{logs / 'app.log'}\n保持此窗口打开；按 Ctrl+C 正常停止服务。停止本服务不会停止 qBittorrent 下载。", flush=True)
            import uvicorn
            from main import app
            server = uvicorn.Server(uvicorn.Config(app, host=settings["server"]["host"], port=settings["server"]["port"], workers=1, log_config=None))
            if not args.no_browser:
                threading.Thread(target=open_when_ready, args=(server, url), daemon=True).start()
            server.run(sockets=[listener])
            if not server.started:
                return 1
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"启动检查失败：{exc}", file=sys.stderr)
        return 1
    except Exception:
        logging.getLogger(__name__).exception("服务启动失败")
        print("服务启动失败，请查看 logs/app.log 或上方日志；检查配置后重试。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
