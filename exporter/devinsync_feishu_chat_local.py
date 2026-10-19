#!/usr/bin/env python3
"""Export Feishu/Lark group chat messages to local files.

This script uses an already-authenticated lark-cli account to fetch chat messages.
It stores configuration, sync state, messages, history, mentions, and attachments
on the local filesystem instead of using a database.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
import uuid
from pathlib import Path

CONFIG_VERSION = 1
DEFAULT_PAGE_SIZE = 50
DEFAULT_TAIL_PAGES = 2
DEFAULT_INTERVAL_SECONDS = 0.0
DEFAULT_OUTPUT_DIR_NAME = "feishu_chat_export"
DEFAULT_CONFIG_NAME = "config.local.json"
ATTACHMENT_MESSAGE_TYPES = {"image", "file", "audio", "video"}
IMAGE_KEY_PATTERN = re.compile(r"\[Image:\s*(img_[^\]\s]+)\]")
ATTACHMENT_ATTR_PATTERN = re.compile(r"([A-Za-z_][\w-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>/]+))")
ATTACHMENT_KEY_PATTERNS = {
    "image_key": re.compile(r'"image_key"\s*:\s*"([^"]+)"'),
    "file_key": re.compile(r'"file_key"\s*:\s*"([^"]+)"'),
    "resource_key": re.compile(r'"resource_key"\s*:\s*"([^"]+)"'),
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def script_dir() -> Path:
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd()


def default_config_path() -> Path:
    return script_dir() / DEFAULT_CONFIG_NAME


def read_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp_path.replace(path)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp_path.replace(path)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def expand_path(value: str | Path) -> Path:
    return Path(str(value)).expanduser().resolve()


def sanitize_file_name(file_name: str | None, fallback: str) -> str:
    name = (file_name or "").strip() or fallback
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = name.strip(" .")
    return name or fallback


def masked(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "..." + value[-4:]


def prompt_text(label: str, current: str | None = None, required: bool = True, secret: bool = False) -> str:
    current = current or ""
    suffix = f" [{masked(current) if secret else current}]" if current else ""
    while True:
        prompt = f"{label}{suffix}: "
        value = getpass.getpass(prompt) if secret else input(prompt)
        value = value.strip()
        if value:
            return value
        if current:
            return current
        if not required:
            return ""
        print("该项不能为空。")


def prompt_int(label: str, current: int) -> int:
    while True:
        value = input(f"{label} [{current}]: ").strip()
        if not value:
            return current
        try:
            return int(value)
        except ValueError:
            print("请输入整数。")


def prompt_float(label: str, current: float) -> float:
    while True:
        value = input(f"{label} [{current}]: ").strip()
        if not value:
            return current
        try:
            return float(value)
        except ValueError:
            print("请输入数字。")


def prompt_bool(label: str, current: bool) -> bool:
    default = "Y/n" if current else "y/N"
    while True:
        value = input(f"{label} [{default}]: ").strip().lower()
        if not value:
            return current
        if value in {"y", "yes", "是", "1", "true"}:
            return True
        if value in {"n", "no", "否", "0", "false"}:
            return False
        print("请输入 y 或 n。")


def detect_lark_cli() -> str:
    native = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@larksuite/cli/bin/lark-cli.exe"
    if sys.platform == "win32" and native.is_file():
        return str(native)
    return shutil.which("lark-cli") or shutil.which("lark-cli.cmd") or "lark-cli"


def resolve_lark_cli(value: str) -> str:
    if not value:
        return detect_lark_cli()
    path = Path(value).expanduser()
    if sys.platform == "win32" and path.suffix.lower() in {".cmd", ".ps1"}:
        native = path.parent / "node_modules/@larksuite/cli/bin/lark-cli.exe"
        if native.is_file():
            return str(native)
    if path.exists():
        return str(path.resolve())
    found = shutil.which(value)
    return found or value


def default_config() -> dict:
    return {
        "config_version": CONFIG_VERSION,
        "chat_id": "",
        "output_dir": str(script_dir() / DEFAULT_OUTPUT_DIR_NAME),
        "lark_cli": detect_lark_cli(),
        "page_size": DEFAULT_PAGE_SIZE,
        "tail_pages": DEFAULT_TAIL_PAGES,
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "download_attachments": True,
        "default_start": "",
    }


def configure_interactively(config: dict, config_path: Path, force: bool) -> dict:
    missing_required = not config.get("chat_id") or not config.get("output_dir")
    if not force and not missing_required:
        return config

    print("首次运行或配置不完整，需要输入以下配置。敏感值会保存到 config.local.json，请勿提交该文件。")
    config["chat_id"] = prompt_text("飞书群 chat_id（输入时不显示）", config.get("chat_id"), secret=True)
    config["output_dir"] = prompt_text("输出文件夹路径", config.get("output_dir") or str(script_dir() / DEFAULT_OUTPUT_DIR_NAME))
    config["lark_cli"] = prompt_text("lark-cli 路径/命令", config.get("lark_cli") or detect_lark_cli())
    config["page_size"] = prompt_int("每页消息数 page_size", int(config.get("page_size") or DEFAULT_PAGE_SIZE))
    config["tail_pages"] = prompt_int("每次回扫最近几页 tail_pages", int(config.get("tail_pages") or DEFAULT_TAIL_PAGES))
    config["interval_seconds"] = prompt_float("持续轮询间隔秒数，0 表示只运行一次", float(config.get("interval_seconds") or 0))
    config["download_attachments"] = prompt_bool("是否下载图片/文件/音频/视频附件", bool(config.get("download_attachments", True)))
    config["default_start"] = prompt_text("首次同步起始时间，可留空，例 2026-06-07 14:00", config.get("default_start"), required=False)
    config["config_version"] = CONFIG_VERSION

    if prompt_bool(f"是否保存以上配置到 {config_path}", True):
        write_json(config_path, config)
        print(f"配置已保存：{config_path}")
    return config


def apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    if args.chat_id:
        config["chat_id"] = args.chat_id
    if args.output_dir:
        config["output_dir"] = args.output_dir
    if args.lark_cli:
        config["lark_cli"] = args.lark_cli
    if args.page_size is not None:
        config["page_size"] = args.page_size
    if args.tail_pages is not None:
        config["tail_pages"] = args.tail_pages
    if args.interval_seconds is not None:
        config["interval_seconds"] = args.interval_seconds
    if args.download_attachments is not None:
        config["download_attachments"] = args.download_attachments
    if args.default_start is not None:
        config["default_start"] = args.default_start
    return config


def validate_config(config: dict) -> dict:
    config["chat_id"] = str(config.get("chat_id") or "").strip()
    config["output_dir"] = str(expand_path(config.get("output_dir") or script_dir() / DEFAULT_OUTPUT_DIR_NAME))
    config["lark_cli"] = resolve_lark_cli(str(config.get("lark_cli") or detect_lark_cli()))
    config["page_size"] = int(config.get("page_size") or DEFAULT_PAGE_SIZE)
    config["tail_pages"] = int(config.get("tail_pages", DEFAULT_TAIL_PAGES))
    config["interval_seconds"] = float(config.get("interval_seconds") or 0)
    config["download_attachments"] = bool(config.get("download_attachments", True))
    config["default_start"] = str(config.get("default_start") or "").strip()
    if not config["chat_id"]:
        raise ValueError("chat_id 不能为空。请运行 --setup 重新配置。")
    if not 1 <= config["page_size"] <= 50:
        raise ValueError("page_size 必须在 1 到 50 之间。")
    if config["tail_pages"] < 0:
        raise ValueError("tail_pages 不能小于 0。")
    if config["interval_seconds"] < 0:
        raise ValueError("interval_seconds 不能小于 0。")
    return config


def check_lark_cli_available(config: dict) -> None:
    command = str(config.get("lark_cli") or "")
    if not command:
        raise RuntimeError("未配置 lark-cli。请运行 --setup 重新配置。")
    command_path = Path(command).expanduser()
    if command_path.exists() or shutil.which(command):
        return
    raise RuntimeError(f"未找到 lark-cli：{command}。请先安装并登录 lark-cli，或运行 --setup 修改路径。")


def ensure_output_layout(output_dir: Path) -> dict[str, Path]:
    paths = {
        "output_dir": output_dir,
        "messages_by_id": output_dir / "messages_by_id.json",
        "messages_json": output_dir / "messages.json",
        "messages_jsonl": output_dir / "messages.jsonl",
        "mentions_jsonl": output_dir / "mentions.jsonl",
        "attachments_jsonl": output_dir / "attachments.jsonl",
        "history_jsonl": output_dir / "message_history.jsonl",
        "state": output_dir / "sync_state.json",
        "log": output_dir / "sync_log.jsonl",
        "attachments_dir": output_dir / "attachments",
        "tmp_dir": output_dir / "_tmp_resources",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    paths["attachments_dir"].mkdir(parents=True, exist_ok=True)
    paths["tmp_dir"].mkdir(parents=True, exist_ok=True)
    return paths


def parse_time(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return dt.datetime.fromtimestamp(number, tz=dt.timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        if number > 10_000_000_000:
            number /= 1000
        return dt.datetime.fromtimestamp(number, tz=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        return normalize_start_time(parsed) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)
    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
    )
    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                return normalize_start_time(parsed)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Unsupported time format: {value}")


def normalize_start_time(parsed: dt.datetime) -> dt.datetime:
    # The CLI renders naive timestamps in local time. Never guess from the date.
    return parsed.astimezone(dt.timezone.utc)


def format_iso_utc(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    else:
        value = value.astimezone(dt.timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_sync_start(records: dict[str, dict], manual_start: str | None, default_start: str | None) -> str | None:
    chosen = manual_start or (default_start if not records else None)
    if chosen:
        return format_iso_utc(parse_time(chosen))
    newest = None
    for record in records.values():
        parsed = parse_time(record.get("create_time"))
        if parsed and (newest is None or parsed > newest):
            newest = parsed
    if newest is None:
        return None
    return format_iso_utc(newest - dt.timedelta(minutes=2))


def sort_key(record: dict):
    parsed = parse_time(record.get("create_time"))
    time_key = parsed.timestamp() if parsed else 0
    position = record.get("message_position") or ""
    return (time_key, str(position), str(record.get("message_id") or ""))


def run_cli(config: dict, args: list[str], cwd: Path | None = None) -> dict:
    command = resolve_lark_cli(config["lark_cli"])
    if sys.platform == "win32" and Path(command).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise RuntimeError("请将 lark_cli 配置为 lark-cli.exe 的完整路径，避免 Windows shell 重新解析参数。")
    gap = max(0.0, float(config.get("request_interval_seconds", 1.2)))
    wait = gap - (time.monotonic() - config.get("_last_request", 0.0))
    if wait > 0:
        time.sleep(wait)
    config["_last_request"] = time.monotonic()
    proc = subprocess.run([command, *args], capture_output=True, text=True,
                          encoding="utf-8", shell=False, cwd=cwd, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"lark-cli 失败（退出码 {proc.returncode}）：\n{proc.stderr or proc.stdout}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("lark-cli 没有返回有效 JSON；未将该页视为空页。") from exc
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise RuntimeError(f"lark-cli 返回失败或无法识别的响应：{data}")
    return data


def run_page(config: dict, start_iso: str | None, page_token: str | None, sort: str = "asc") -> dict:
    cmd = [
        "im",
        "+chat-messages-list",
        "--as",
        "user",
        "--chat-id",
        config["chat_id"],
        "--order",
        sort,
        "--page-size",
        str(config["page_size"]),
        "--no-reactions",
        "--format",
        "json",
    ]
    if start_iso:
        cmd.extend(["--start", start_iso])
    if config.get("sync_end_time"):
        cmd.extend(["--end", config["sync_end_time"]])
    if page_token:
        cmd.extend(["--page-token", page_token])

    return run_cli(config, cmd)


def payload_messages(data: dict) -> tuple[list[dict], bool, str | None]:
    payload = data.get("data")
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise RuntimeError("响应缺少 data.messages 数组，停止以避免误报已导出全部记录。")
    messages = payload["messages"]
    if any(not isinstance(m, dict) or not m.get("message_id") for m in messages):
        raise RuntimeError("消息响应中存在缺失 message_id 的记录。")
    return messages, bool(payload.get("has_more")), payload.get("page_token")


def parse_message_content(message: dict) -> dict | None:
    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def extract_mentions(message: dict) -> list[dict]:
    payload = parse_message_content(message) or {}
    mentions = message.get("mentions") or payload.get("mentions") or []
    if not isinstance(mentions, list):
        return []
    rows = []
    message_id = message.get("message_id")
    for idx, item in enumerate(mentions):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "message_id": message_id,
                "mention_index": idx,
                "mention_name": item.get("name"),
                "mention_id": item.get("id") or item.get("open_id") or item.get("user_id"),
                "mention_id_type": item.get("id_type"),
                "raw_json": item,
            }
        )
    return rows


def parse_attachment_markup(content: str | None) -> dict:
    if not isinstance(content, str) or not content:
        return {}
    attrs = {}
    for match in ATTACHMENT_ATTR_PATTERN.finditer(content):
        key = match.group(1)
        value = match.group(2) or match.group(3) or match.group(4) or ""
        attrs[key] = value
    for key, pattern in ATTACHMENT_KEY_PATTERNS.items():
        if key not in attrs:
            match = pattern.search(content)
            if match:
                attrs[key] = match.group(1)
    if "image_key" not in attrs:
        match = IMAGE_KEY_PATTERN.search(content)
        if match:
            attrs["image_key"] = match.group(1)
    return attrs


def find_nested_value(data, keys: tuple[str, ...]):
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                return value
        for value in data.values():
            found = find_nested_value(value, keys)
            if found not in (None, ""):
                return found
    if isinstance(data, list):
        for item in data:
            found = find_nested_value(item, keys)
            if found not in (None, ""):
                return found
    return None


def normalize_attachment_size(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def extract_attachment_candidates(message: dict) -> list[dict]:
    msg_type = message.get("msg_type")
    if msg_type not in ATTACHMENT_MESSAGE_TYPES | {"media", "post", "merge_forward", "folder"}:
        return []

    payload = parse_message_content(message) or {}
    content = message.get("content") if isinstance(message.get("content"), str) else None
    markup = parse_attachment_markup(content)
    raw_payload = payload if payload else {"content": content} if content else {}
    candidates = []

    # Current CLI renders resources as Markdown images and XML-like tags.
    # Parse each marker separately so a rich-text post can contain many files.
    if content:
        refs = [(m.group(1), "image", {}) for m in re.finditer(r'!\[[^\]]*\]\((img_[^\s)]+)\)', content)]
        for match in re.finditer(r'<(image|img|file|audio|video|media)\b([^>]*)>', content):
            attrs = parse_attachment_markup(match.group(2))
            key = attrs.get("key") or attrs.get("image_key") or attrs.get("file_key")
            if key:
                refs.append((key, "image" if match.group(1) in {"image", "img"} else "file", attrs))
        seen = set()
        for key, kind, attrs in refs:
            if key in seen:
                continue
            seen.add(key)
            candidates.append({
                "attachment_index": len(candidates), "attachment_type": msg_type,
                "resource_type": kind, "resource_key": key,
                "file_name": attrs.get("name") or attrs.get("file_name"),
                "mime_type": attrs.get("mime_type"),
                "file_size": normalize_attachment_size(attrs.get("size")),
                "raw_json": attrs,
            })
        if candidates:
            return candidates

    if msg_type == "image":
        image_key = (
            payload.get("image_key")
            or payload.get("file_key")
            or markup.get("image_key")
            or markup.get("file_key")
            or find_nested_value(message, ("image_key", "file_key", "resource_key"))
        )
        if image_key:
            candidates.append(
                {
                    "attachment_index": 0,
                    "attachment_type": "image",
                    "resource_type": "image",
                    "resource_key": image_key,
                    "file_name": payload.get("file_name") or payload.get("image_name") or markup.get("file_name") or markup.get("image_name"),
                    "mime_type": payload.get("content_type") or payload.get("mime_type") or markup.get("content_type") or markup.get("mime_type"),
                    "file_size": normalize_attachment_size(payload.get("file_size") or payload.get("size") or markup.get("file_size") or markup.get("size") or find_nested_value(message, ("file_size", "size"))),
                    "raw_json": raw_payload,
                }
            )
        return candidates

    resource_key = (
        payload.get("file_key")
        or payload.get("resource_key")
        or markup.get("file_key")
        or markup.get("resource_key")
        or find_nested_value(message, ("file_key", "resource_key", "image_key"))
    )
    if not resource_key:
        return []
    candidates.append(
        {
            "attachment_index": 0,
            "attachment_type": msg_type,
            "resource_type": "file",
            "resource_key": resource_key,
            "file_name": payload.get("file_name") or payload.get("name") or markup.get("file_name") or markup.get("name"),
            "mime_type": payload.get("mime_type") or payload.get("content_type") or markup.get("mime_type") or markup.get("content_type"),
            "file_size": normalize_attachment_size(payload.get("file_size") or payload.get("size") or markup.get("file_size") or markup.get("size") or find_nested_value(message, ("file_size", "size"))),
            "raw_json": raw_payload,
        }
    )
    return candidates


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def guess_suffix(path: Path, file_name: str | None, mime_type: str | None) -> str:
    suffix = Path(file_name or path.name).suffix
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "")
    return guessed or path.suffix or ".bin"


def download_message_resource(config: dict, paths: dict[str, Path], message_id: str, resource_key: str, resource_type: str) -> tuple[dict, Path]:
    paths["tmp_dir"].mkdir(parents=True, exist_ok=True)
    temp_name = f"{resource_type}_{sanitize_file_name(message_id, 'message')}_{uuid.uuid4().hex}.bin"
    output_path = paths["tmp_dir"] / temp_name
    cmd = [
        "im",
        "+messages-resources-download",
        "--as",
        "user",
        "--message-id",
        message_id,
        "--file-key",
        resource_key,
        "--type",
        "image" if resource_type == "image" else "file",
        "--output",
        output_path.relative_to(paths["output_dir"]).as_posix(),
        "--format",
        "json",
    ]
    payload = run_cli(config, cmd, cwd=paths["output_dir"])
    returned_path = (payload.get("data") or {}).get("saved_path") or str(output_path)
    abs_path = Path(returned_path)
    if not abs_path.is_absolute():
        abs_path = paths["output_dir"] / abs_path
    if not abs_path.exists():
        raise FileNotFoundError(f"downloaded resource not found: {abs_path}")
    return payload, abs_path


def store_attachment(paths: dict[str, Path], message_id: str, candidate: dict, abs_path: Path) -> dict:
    sha256 = sha256_file(abs_path)
    mime_type = candidate.get("mime_type") or mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"
    suffix = guess_suffix(abs_path, candidate.get("file_name"), mime_type)
    fallback = f"{candidate.get('attachment_type') or 'attachment'}_{candidate.get('attachment_index', 0)}{suffix}"
    safe_name = sanitize_file_name(candidate.get("file_name"), fallback)
    if not Path(safe_name).suffix:
        safe_name += suffix

    message_dir = paths["attachments_dir"] / sanitize_file_name(message_id, "message")
    message_dir.mkdir(parents=True, exist_ok=True)
    destination = message_dir / safe_name
    if destination.exists():
        if sha256_file(destination) == sha256:
            abs_path.unlink(missing_ok=True)
        else:
            stem = destination.stem
            destination = destination.with_name(f"{stem}_{sha256[:8]}{destination.suffix}")
            if not destination.exists():
                shutil.move(str(abs_path), str(destination))
            else:
                abs_path.unlink(missing_ok=True)
    else:
        shutil.move(str(abs_path), str(destination))

    relative_path = destination.relative_to(paths["output_dir"])
    return {
        "storage_backend": "local",
        "storage_path": relative_path.as_posix(),
        "absolute_path": str(destination),
        "sha256": sha256,
        "mime_type": mime_type,
        "file_size": destination.stat().st_size,
    }


def download_attachments(config: dict, paths: dict[str, Path], message: dict, existing_record: dict | None) -> tuple[list[dict], int, int]:
    message_id = str(message.get("message_id") or "")
    if not message_id:
        return [], 0, 0

    old_attachments = existing_record.get("attachments", []) if existing_record else []
    old_by_key = {
        (item.get("resource_key"), item.get("attachment_index")): item
        for item in old_attachments
        if isinstance(item, dict)
    }
    attachments = []
    downloaded = 0
    failed = 0
    candidates = extract_attachment_candidates(message)
    if not config.get("download_attachments"):
        for candidate in candidates:
            existing = old_by_key.get((candidate.get("resource_key"), candidate.get("attachment_index")))
            if existing and existing.get("download_status") == "downloaded":
                attachments.append(existing)
                continue
            row = dict(candidate)
            row.update({"message_id": message_id, "download_status": "skipped", "download_error": None})
            attachments.append(row)
        return attachments, downloaded, failed

    for candidate in candidates:
        row = dict(candidate)
        row["message_id"] = message_id
        existing = old_by_key.get((row.get("resource_key"), row.get("attachment_index")))
        if existing and existing.get("download_status") == "downloaded" and existing.get("storage_path"):
            existing_path = paths["output_dir"] / existing["storage_path"]
            if existing_path.exists():
                attachments.append(existing)
                continue
        try:
            payload, abs_path = download_message_resource(config, paths, message_id, str(row["resource_key"]), str(row["resource_type"]))
            stored = store_attachment(paths, message_id, row, abs_path)
            row.update(stored)
            row["download_status"] = "downloaded"
            row["download_error"] = None
            row["download_payload"] = payload
            downloaded += 1
        except Exception as exc:
            row["download_status"] = "failed"
            row["download_error"] = str(exc)
            failed += 1
        attachments.append(row)
    return attachments, downloaded, failed


def determine_sync_status(message: dict) -> str:
    if bool(message.get("deleted", False)):
        return "recalled"
    if bool(message.get("updated", False)):
        return "updated"
    return "normal"


def history_event(message_id: str, event_type: str, source: str, message: dict, previous: dict | None, note: str | None = None) -> dict:
    return {
        "event_at": now_iso(),
        "message_id": message_id,
        "event_type": event_type,
        "sync_source": source,
        "deleted": bool(message.get("deleted", False)),
        "updated": bool(message.get("updated", False)),
        "is_recalled": bool(message.get("deleted", False)),
        "previous_content_snapshot": previous.get("current_content") if previous else None,
        "content_snapshot": message.get("content"),
        "previous_raw_json_snapshot": previous.get("raw_json_latest") if previous else None,
        "raw_json_snapshot": message,
        "note": note,
    }


def build_record(message: dict, existing: dict | None, source: str, attachments: list[dict]) -> tuple[dict, list[dict], str]:
    message_id = str(message.get("message_id") or "")
    sender = message.get("sender") or {}
    content = message.get("content")
    deleted = bool(message.get("deleted", False))
    updated = bool(message.get("updated", False))
    sync_status = determine_sync_status(message)
    timestamp = now_iso()
    events = []

    if existing is None:
        record = {
            "message_id": message_id,
            "chat_id": message.get("chat_id"),
            "create_time": message.get("create_time"),
            "message_position": message.get("message_position"),
            "msg_type": message.get("msg_type"),
            "content": content,
            "original_content": content,
            "current_content": content,
            "deleted": deleted,
            "updated": updated,
            "is_recalled": deleted,
            "recalled_at": timestamp if deleted else None,
            "recall_detected_at": timestamp if deleted else None,
            "first_seen_at": timestamp,
            "last_seen_at": timestamp,
            "sync_status": sync_status,
            "message_app_link": message.get("message_app_link"),
            "sender_id": sender.get("id"),
            "sender_name": sender.get("name"),
            "sender_id_type": sender.get("id_type"),
            "sender_type": sender.get("sender_type"),
            "sender_tenant_key": sender.get("tenant_key"),
            "mentions": extract_mentions(message),
            "attachments": attachments,
            "raw_json_first": message,
            "raw_json_latest": message,
        }
        events.append(history_event(message_id, "created", source, message, None, f"source={source}"))
        if deleted:
            events.append(history_event(message_id, "recalled", source, message, None, f"message already recalled when first seen; source={source}"))
        return record, events, "inserted"

    prev_recalled = bool(existing.get("is_recalled"))
    changed = (
        existing.get("current_content") != content
        or bool(existing.get("deleted")) != deleted
        or bool(existing.get("updated")) != updated
        or prev_recalled != deleted
    )
    record = dict(existing)
    record.update(
        {
            "chat_id": message.get("chat_id"),
            "create_time": message.get("create_time"),
            "message_position": message.get("message_position"),
            "msg_type": message.get("msg_type"),
            "content": content,
            "current_content": content,
            "deleted": deleted,
            "updated": updated,
            "is_recalled": deleted,
            "last_seen_at": timestamp,
            "sync_status": sync_status,
            "message_app_link": message.get("message_app_link"),
            "sender_id": sender.get("id"),
            "sender_name": sender.get("name") or existing.get("sender_name"),
            "sender_id_type": sender.get("id_type"),
            "sender_type": sender.get("sender_type"),
            "sender_tenant_key": sender.get("tenant_key"),
            "mentions": extract_mentions(message),
            "attachments": attachments,
            "raw_json_latest": message,
        }
    )
    if deleted and not existing.get("recalled_at"):
        record["recalled_at"] = timestamp
    if deleted and not existing.get("recall_detected_at"):
        record["recall_detected_at"] = timestamp
    if changed:
        event_type = "recalled" if deleted and not prev_recalled else "updated"
        events.append(history_event(message_id, event_type, source, message, existing, f"source={source}"))
        return record, events, "updated"
    return record, events, "unchanged"


def upsert_message(config: dict, paths: dict[str, Path], records: dict[str, dict], message: dict, source: str) -> tuple[str, int, int, list[dict]]:
    message = dict(message)
    message.setdefault("chat_id", config.get("chat_id"))
    message_id = str(message.get("message_id") or "")
    if not message_id:
        return "skipped", 0, 0, []
    existing = records.get(message_id)
    attachments, downloaded, failed = download_attachments(config, paths, message, existing)
    record, events, status = build_record(message, existing, source, attachments)
    record["thread_id"] = message.get("thread_id") or (existing or {}).get("thread_id")
    records[message_id] = record
    return status, downloaded, failed, events


def load_records(paths: dict[str, Path]) -> dict[str, dict]:
    records = read_json(paths["messages_by_id"], {})
    if isinstance(records, dict):
        # Replay completed pages even if the process stopped before final exports.
        for page_path in sorted((paths["output_dir"] / "checkpoints").glob("*.json")):
            page = read_json(page_path, {})
            for record in page.get("records", []):
                records[record["message_id"]] = record
        return records
    raise RuntimeError("本地消息索引损坏：messages_by_id.json 必须为对象。")


def save_page(paths: dict[str, Path], records: dict[str, dict], ids: list[str], events: list[dict]) -> None:
    # A single atomic page file is the recovery source; the next run merges it.
    name = f"{time.time_ns():020d}_{uuid.uuid4().hex}.json"
    write_json(paths["output_dir"] / "checkpoints" / name,
               {"records": [records[mid] for mid in dict.fromkeys(ids)], "events": events})
    append_history(paths, events)


def sync_threads(config: dict, paths: dict[str, Path], records: dict[str, dict]) -> dict:
    thread_ids = sorted({r.get("thread_id") for r in records.values() if r.get("thread_id")})
    cutoff = parse_time(config.get("thread_start"))
    end = parse_time(config.get("sync_end_time"))
    # Old threads may receive new replies even if their root predates the range.
    # Descending pages let a bounded increment avoid rereading entire old threads.
    progress_path = paths["output_dir"] / "thread_progress.json"
    signature = [config.get("thread_start"), config.get("sync_end_time")]
    progress = read_json(progress_path, {}) if cutoff else {}
    completed = set(progress.get("completed", [])) if progress.get("range") == signature else set()
    count = 0
    for index, thread_id in enumerate(thread_ids, 1):
        if thread_id in completed:
            continue
        token = None
        seen_tokens = set()
        while True:
            cmd = ["im", "+threads-messages-list", "--as", "user", "--thread", thread_id,
                   "--order", "desc" if cutoff else "asc", "--page-size", str(config["page_size"]), "--no-reactions", "--format", "json"]
            if token:
                cmd.extend(["--page-token", token])
            messages, has_more, token = payload_messages(run_cli(config, cmd))
            events, ids = [], []
            page_times = []
            for message in messages:
                timestamp = parse_time(message.get("create_time"))
                if timestamp:
                    page_times.append(timestamp)
                if cutoff and timestamp and timestamp < cutoff:
                    continue
                if end and timestamp and timestamp > end:
                    continue
                message.setdefault("thread_id", thread_id)
                _, _, _, new_events = upsert_message(config, paths, records, message, "thread")
                events.extend(new_events)
                ids.append(message["message_id"])
                count += 1
            save_page(paths, records, ids, events)
            if cutoff and len(page_times) == len(messages) and page_times and min(page_times) < cutoff:
                break
            if not has_more:
                break
            if not token or token in seen_tokens:
                raise RuntimeError("话题分页游标缺失或重复，未完成全量导出。")
            seen_tokens.add(token)
        if cutoff:
            completed.add(thread_id)
            write_json(progress_path, {"range": signature, "completed": sorted(completed)})
        print(f"话题 {index}/{len(thread_ids)} 已保存；本地共 {len(records)} 条消息。", flush=True)
    return {"threads_fetched": len(thread_ids), "thread_messages_fetched": count}


def flatten_mentions(records: dict[str, dict]) -> list[dict]:
    rows = []
    for record in records.values():
        for mention in record.get("mentions") or []:
            rows.append(mention)
    return rows


def flatten_attachments(records: dict[str, dict]) -> list[dict]:
    rows = []
    for record in records.values():
        for attachment in record.get("attachments") or []:
            rows.append(attachment)
    return rows


def write_outputs(paths: dict[str, Path], records: dict[str, dict]) -> None:
    rows = sorted(records.values(), key=sort_key)
    write_json(paths["messages_by_id"], records)
    write_json(paths["messages_json"], rows)
    write_jsonl(paths["messages_jsonl"], rows)
    write_jsonl(paths["mentions_jsonl"], flatten_mentions(records))
    write_jsonl(paths["attachments_jsonl"], flatten_attachments(records))


def append_history(paths: dict[str, Path], events: list[dict]) -> None:
    for event in events:
        append_jsonl(paths["history_jsonl"], event)


def sync_recent_tail(config: dict, paths: dict[str, Path], records: dict[str, dict], tail_pages: int) -> dict:
    if tail_pages <= 0:
        return {"tail_pages_fetched": 0, "tail_messages_fetched": 0, "tail_updated_messages": 0, "tail_downloaded_attachments": 0, "tail_failed_attachments": 0}

    page_token = None
    has_more = True
    pages_fetched = 0
    fetched_messages = 0
    updated_messages = 0
    downloaded_attachments = 0
    failed_attachments = 0
    all_events = []
    seen_tokens = set()

    while has_more and pages_fetched < tail_pages:
        data = run_page(config, None, page_token, sort="desc")
        pages_fetched += 1
        messages, has_more, page_token = payload_messages(data)
        page_ids = []
        for message in messages:
            fetched_messages += 1
            status, downloaded, failed, events = upsert_message(config, paths, records, message, "tail_refresh")
            if status in {"inserted", "updated", "unchanged"}:
                updated_messages += 1
            downloaded_attachments += downloaded
            failed_attachments += failed
            all_events.extend(events)
            page_ids.append(message["message_id"])
        save_page(paths, records, page_ids, all_events)
        all_events = []
        if has_more and page_token in seen_tokens:
            raise RuntimeError("最近消息分页游标重复。")
        seen_tokens.add(page_token)
        if has_more and not page_token and pages_fetched < tail_pages:
            raise RuntimeError("has_more=true but no page_token returned during tail refresh")

    append_history(paths, all_events)
    return {
        "tail_pages_fetched": pages_fetched,
        "tail_messages_fetched": fetched_messages,
        "tail_updated_messages": updated_messages,
        "tail_downloaded_attachments": downloaded_attachments,
        "tail_failed_attachments": failed_attachments,
    }


def sync_once(config: dict, paths: dict[str, Path], manual_start: str | None) -> dict:
    try:
        return _sync_once(config, paths, manual_start)
    except (Exception, KeyboardInterrupt) as exc:
        state = read_json(paths["state"], {})
        state.update({"status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                      "message_pagination_complete": False, "stopped_at": now_iso(),
                      "error_type": type(exc).__name__, "error": str(exc)})
        write_json(paths["state"], state)
        raise


def _sync_once(config: dict, paths: dict[str, Path], manual_start: str | None) -> dict:
    records = load_records(paths)
    if any(r.get("chat_id") and r["chat_id"] != config["chat_id"] for r in records.values()):
        raise RuntimeError("此输出目录包含其他群的数据，请为目标群使用独立目录。")
    start_iso = None if config.get("full_history") else get_sync_start(records, manual_start, config.get("default_start"))
    write_json(paths["state"], {"status": "running", "started_at": now_iso(),
                              "chat_id_masked": masked(config["chat_id"]), "start_used": start_iso})
    inserted_messages = 0
    updated_messages = 0
    unchanged_messages = 0
    downloaded_attachments = 0
    failed_attachments = 0
    fetched_messages = 0
    pages_fetched = 0
    page_token = None
    has_more = True
    newest_time = None
    newest_position = None
    all_events = []
    seen_tokens = set()

    while has_more:
        data = run_page(config, start_iso, page_token, sort="asc")
        pages_fetched += 1
        messages, has_more, page_token = payload_messages(data)
        page_ids = []
        for message in messages:
            fetched_messages += 1
            status, downloaded, failed, events = upsert_message(config, paths, records, message, "incremental")
            if status == "inserted":
                inserted_messages += 1
            elif status == "updated":
                updated_messages += 1
            elif status == "unchanged":
                unchanged_messages += 1
            downloaded_attachments += downloaded
            failed_attachments += failed
            all_events.extend(events)
            page_ids.append(message["message_id"])
            newest_time = message.get("create_time") or newest_time
            newest_position = message.get("message_position") or newest_position
        save_page(paths, records, page_ids, all_events)
        all_events = []
        print(f"群消息第 {pages_fetched} 页已保存；累计 {len(records)} 条。", flush=True)
        if has_more and page_token in seen_tokens:
            raise RuntimeError("群消息分页游标重复，未完成全量导出。")
        seen_tokens.add(page_token)
        if has_more and not page_token:
            raise RuntimeError("has_more=true but no page_token returned")

    append_history(paths, all_events)
    tail_result = sync_recent_tail(config, paths, records, int(config["tail_pages"]))
    thread_result = sync_threads(config, paths, records)
    write_outputs(paths, records)

    # Group pagination excludes replies; its final row can be older than the
    # newest reply. Persist the real maximum so the next increment cannot regress.
    newest_record = max(records.values(), key=sort_key) if records else {}
    newest_time = newest_record.get("create_time")
    newest_position = newest_record.get("message_position")

    total_attachments = len(flatten_attachments(records))
    downloaded_attachments_total = sum(1 for item in flatten_attachments(records) if item.get("download_status") == "downloaded")
    recalled_messages = sum(1 for item in records.values() if item.get("is_recalled"))
    state = {
        "status": "completed",
        "message_pagination_complete": True,
        "full_history_requested": bool(config.get("full_history")),
        "last_run_at": now_iso(),
        "chat_id_masked": masked(config["chat_id"]),
        "start_used": start_iso,
        "newest_time": newest_time,
        "newest_position": newest_position,
        "total_messages": len(records),
        "total_attachments": total_attachments,
        "downloaded_attachments_total": downloaded_attachments_total,
        "recalled_messages": recalled_messages,
        "failed_attachments_total": sum(1 for item in flatten_attachments(records) if item.get("download_status") == "failed"),
    }
    write_json(paths["state"], state)

    result = {
        "chat_id_masked": masked(config["chat_id"]),
        "output_dir": str(paths["output_dir"]),
        "start_used": start_iso,
        "pages_fetched": pages_fetched,
        "fetched_messages": fetched_messages,
        "inserted_messages": inserted_messages,
        "updated_messages": updated_messages,
        "unchanged_messages": unchanged_messages,
        "downloaded_attachments": downloaded_attachments,
        "failed_attachments": failed_attachments,
        "total_messages": len(records),
        "total_attachments": total_attachments,
        "downloaded_attachments_total": downloaded_attachments_total,
        "recalled_messages": recalled_messages,
        "newest_time": newest_time,
        "newest_position": newest_position,
    }
    result.update(tail_result)
    result.update(thread_result)
    append_jsonl(paths["log"], {"run_at": now_iso(), **result})
    return result


def print_output_summary(paths: dict[str, Path]) -> None:
    print("\n输出文件：")
    for key in ("messages_jsonl", "messages_json", "messages_by_id", "mentions_jsonl", "attachments_jsonl", "history_jsonl", "state", "log"):
        print(f"- {key}: {paths[key]}")
    print(f"- attachments_dir: {paths['attachments_dir']}")


def run_self_test() -> None:
    sample_message = {
        "message_id": "om_test_1",
        "chat_id": "oc_test",
        "create_time": "2026-06-07 14:00:00",
        "message_position": "1",
        "msg_type": "text",
        "content": json.dumps({"text": "hello @user", "mentions": [{"name": "user", "id": "ou_x", "id_type": "open_id"}]}, ensure_ascii=False),
        "sender": {"id": "ou_sender", "name": "sender", "id_type": "open_id", "sender_type": "user", "tenant_key": "tenant"},
        "deleted": False,
        "updated": False,
    }
    tmp_dir = Path(tempfile.mkdtemp(prefix="feishu-self-test-"))
    paths = ensure_output_layout(tmp_dir)
    config = default_config()
    config["download_attachments"] = False
    records = {}
    status, downloaded, failed, events = upsert_message(config, paths, records, sample_message, "self_test")
    assert status == "inserted"
    assert downloaded == 0
    assert failed == 0
    assert events
    write_outputs(paths, records)
    assert paths["messages_jsonl"].exists()
    assert paths["mentions_jsonl"].exists()
    shutil.rmtree(tmp_dir)
    print("self-test passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Feishu/Lark group chat messages to local files.")
    parser.add_argument("--config", default=str(default_config_path()), help="config file path, default: ./config.local.json")
    parser.add_argument("--setup", action="store_true", help="run interactive setup and save config")
    parser.add_argument("--chat-id", help="Feishu/Lark chat_id; overrides config")
    parser.add_argument("--output-dir", help="local output directory; overrides config")
    parser.add_argument("--lark-cli", help="lark-cli path/command; overrides config")
    parser.add_argument("--page-size", type=int, help="messages per page; overrides config")
    parser.add_argument("--tail-pages", type=int, help="refresh latest N pages to catch edits/recalls; overrides config")
    parser.add_argument("--interval-seconds", type=float, help="when > 0, keep looping with this poll interval; overrides config")
    parser.add_argument("--start", help="manual start time for this run, e.g. 2026-06-07 14:00")
    parser.add_argument("--default-start", help="saved default start time for first sync; empty means fetch from earliest available")
    parser.add_argument("--download-attachments", dest="download_attachments", action="store_true", default=None, help="download image/file/audio/video resources")
    parser.add_argument("--no-download-attachments", dest="download_attachments", action="store_false", help="do not download resources")
    parser.add_argument("--print-paths", action="store_true", help="print output file paths before syncing")
    parser.add_argument("--self-test", action="store_true", help="run a local self-test without calling lark-cli")
    parser.add_argument("--full-history", action="store_true", help="scan all available group history and all thread pages")
    return parser.parse_args()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parse_args()
    if args.self_test:
        run_self_test()
        return

    config_path = expand_path(args.config)
    config = default_config()
    config.update(read_json(config_path, {}))
    config = apply_cli_overrides(config, args)
    config = configure_interactively(config, config_path, args.setup)
    config = validate_config(config)
    config["full_history"] = args.full_history
    check_lark_cli_available(config)
    paths = ensure_output_layout(expand_path(config["output_dir"]))

    if args.print_paths:
        print_output_summary(paths)

    while True:
        result = sync_once(config, paths, args.start)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print_output_summary(paths)
        if float(config["interval_seconds"]) <= 0:
            break
        args.start = None
        time.sleep(float(config["interval_seconds"]))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        raise SystemExit(130)
