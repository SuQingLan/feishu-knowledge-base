"""Verify saved export consistency and attachment hashes without network calls."""
import collections
import json
from pathlib import Path

import devinsync_feishu_chat_local as exporter


def main():
    config = exporter.read_json(exporter.default_config_path(), {})
    root = Path(config["output_dir"])
    records = exporter.read_json(root / "messages_by_id.json", {})
    rows = exporter.read_json(root / "messages.json", [])
    with (root / "messages.jsonl").open(encoding="utf-8") as handle:
        jsonl_rows = [json.loads(line) for line in handle if line.strip()]
    assert rows == jsonl_rows, "JSON and JSONL exports differ"
    assert len(rows) == len(records) == len({row["message_id"] for row in rows}), "Duplicate/missing message IDs"
    assert {row["message_id"]: row for row in rows} == records, "Index differs from export"
    assert all(row["chat_id"] == config["chat_id"] for row in rows), "Unexpected chat ID"
    attachments = exporter.flatten_attachments(records)
    errors = []
    verified_bytes = 0
    for item in attachments:
        if item.get("download_status") != "downloaded":
            continue
        path = (root / item["storage_path"]).resolve()
        if not path.is_relative_to(root.resolve()):
            errors.append({"path": item["storage_path"], "error": "outside export directory"})
        elif not path.is_file():
            errors.append({"path": item["storage_path"], "error": "missing"})
        elif exporter.sha256_file(path) != item["sha256"]:
            errors.append({"path": item["storage_path"], "error": "sha256 mismatch"})
        else:
            verified_bytes += path.stat().st_size
    state = exporter.read_json(root / "sync_state.json", {})
    summary = {
        "verified_at": exporter.now_iso(), "sync_status": state.get("status"),
        "message_pagination_complete": state.get("message_pagination_complete", False),
        "full_history_requested": state.get("full_history_requested", False),
        "total_messages": len(rows),
        "first_message_time": rows[0]["create_time"] if rows else None,
        "last_message_time": rows[-1]["create_time"] if rows else None,
        "message_types": dict(collections.Counter(row["msg_type"] for row in rows)),
        "threads": len({row.get("thread_id") for row in rows if row.get("thread_id")}),
        "attachment_status": dict(collections.Counter(item.get("download_status") for item in attachments)),
        "verified_attachment_bytes": verified_bytes,
        "attachment_integrity_errors": errors,
        "scope": "All pages returned to the authorized account; unavailable/deleted content and unsupported resources cannot be recovered.",
    }
    exporter.write_json(root / "verification_summary.json", summary)
    with (root / "chat_history.txt").open("w", encoding="utf-8") as handle:
        handle.write("飞书群聊天记录（按时间排序；包含已读取的话题回复）\n\n")
        for row in rows:
            sender = row.get("sender_name") or row.get("sender_id") or "未知发送者"
            handle.write(f"[{row.get('create_time', '')}] {sender} [{row.get('msg_type', '')}]\n")
            handle.write(f"消息ID：{row['message_id']}\n")
            if row.get("thread_id"):
                handle.write(f"话题ID：{row['thread_id']}\n")
            if row.get("is_recalled"):
                handle.write("[接口标记为已撤回]\n")
            content = row.get("current_content")
            handle.write((content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)) + "\n")
            for item in row.get("attachments", []):
                handle.write(f"附件：{item.get('storage_path') or item.get('resource_key')} [{item.get('download_status')}]\n")
            handle.write("\n" + "-" * 72 + "\n\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if errors or state.get("status") != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
