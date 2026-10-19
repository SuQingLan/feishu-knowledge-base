"""Freeze a bounded increment without modifying the preceding export."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import devinsync_feishu_chat_local as e

ROOT = Path(__file__).resolve().parent
# 占位默认值；实际使用请显式传 --base / --output-dir，不含任何真实群信息
DEFAULT_BASE = ROOT / 'exports/last-increment'
DEFAULT_DEST = ROOT / 'exports/increment-next'

def main(base=None, destination=None, start_time=None, end_time=None):
    base = Path(base).resolve() if base else DEFAULT_BASE
    dest = Path(destination).resolve() if destination else DEFAULT_DEST
    if base == dest or dest.is_relative_to(base) or base.is_relative_to(dest):
        raise ValueError('Base and destination must be separate export directories')
    cfg = e.validate_config({**e.default_config(), **e.read_json(ROOT/'config.local.json', {})})
    paths = e.ensure_output_layout(dest)
    manifest_path = dest/'increment_manifest.json'
    manifest = e.read_json(manifest_path, {})
    if not manifest:
        rows = e.read_json(base/'messages.json', [])
        if not rows:
            raise ValueError('Base export is empty or missing messages.json')
        newest = max(rows, key=e.sort_key)
        previous_manifest = e.read_json(base/'increment_manifest.json', {})
        start = e.format_iso_utc(e.parse_time(start_time or previous_manifest.get('end') or newest['create_time']))
        end = e.format_iso_utc(e.parse_time(end_time)) if end_time else e.format_iso_utc(dt.datetime.now(dt.timezone.utc))
        if e.parse_time(start) >= e.parse_time(end):
            raise ValueError('Increment start must precede end')
        manifest = {'base_export': str(base), 'base_sha256': hashlib.sha256((base/'messages.json').read_bytes()).hexdigest(),
                    'base_count': len(rows), 'base_latest_message': newest['message_id'],
                    'start': start, 'end': end, 'destination': str(dest), 'created': e.now_iso(),
                    'attachment_cache': str(base/'attachments')}
        # Point cached resources back to immutable originals instead of copying
        # over a gigabyte or downloading them again. New resources stay in DEST.
        for row in rows:
            for item in row.get('attachments', []):
                if item.get('storage_path'):
                    cached_path = (base/item['storage_path']).resolve()
                    try:
                        item['storage_path'] = Path(os.path.relpath(cached_path, dest)).as_posix()
                    except ValueError:
                        # Separate Windows drives cannot have a relative path.
                        item['storage_path'] = str(cached_path)
        e.write_json(paths['messages_by_id'], {r['message_id']:r for r in rows})
        e.write_json(manifest_path, manifest)
    if Path(manifest['base_export']).resolve() != base or Path(manifest['destination']).resolve() != dest:
        raise ValueError('Existing destination manifest belongs to another base or directory')
    for requested, key in ((start_time, 'start'), (end_time, 'end')):
        if requested and e.parse_time(requested) != e.parse_time(manifest[key]):
            raise ValueError('Existing destination has different frozen time boundaries')
    assert hashlib.sha256((base/'messages.json').read_bytes()).hexdigest() == manifest['base_sha256']
    cfg.update(output_dir=str(dest), interval_seconds=0, full_history=False,
               sync_end_time=manifest['end'], thread_start=manifest['start'])
    print(json.dumps(manifest, ensure_ascii=False), flush=True)
    result = e.sync_once(cfg, paths, manifest['start'])
    old = e.read_json(base/'messages_by_id.json', {})
    records = e.read_json(paths['messages_by_id'], {})
    delta = sorted((r for mid,r in records.items() if mid not in old), key=e.sort_key)
    e.write_json(dest/'new_messages.json', delta)
    e.write_jsonl(dest/'new_messages.jsonl', delta)
    # Observational timestamps change during refresh; semantic comparison does not.
    fields = ['current_content','is_recalled','is_edited','msg_type','mentions']
    changed = [dict(message_id=mid, fields=[k for k in fields if old[mid].get(k)!=r.get(k)])
               for mid,r in records.items() if mid in old and any(old[mid].get(k)!=r.get(k) for k in fields)]
    e.write_json(dest/'updated_existing_messages.json', changed)
    result.update(new_unique_messages=len(delta), existing_semantic_updates=len(changed))
    e.write_json(dest/'increment_result.json',result)
    assert hashlib.sha256((base/'messages.json').read_bytes()).hexdigest() == manifest['base_sha256']
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', help='preceding frozen export directory')
    parser.add_argument('--output-dir', help='new, separate destination directory')
    parser.add_argument('--start', help='explicit start; defaults to preceding batch end, then latest message')
    parser.add_argument('--end', help='freeze cutoff, defaults to current UTC time')
    args = parser.parse_args()
    main(args.base, args.output_dir, args.start, args.end)
