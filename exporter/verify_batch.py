"""Validate an immutable increment and preserve stable cross-batch citations."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import devinsync_feishu_chat_local as e
from clean_chat import redact, semantic_text


def lines(path):
    return [json.loads(s) for s in Path(path).read_text(encoding='utf-8-sig').splitlines() if s.strip()]


def verify(dest, output, old_audits):
    dest, output = Path(dest).resolve(), Path(output).resolve()
    manifest = e.read_json(dest/'increment_manifest.json', {})
    base = Path(manifest['base_export'])
    assert e.sha256_file(base/'messages.json') == manifest['base_sha256']
    before = e.read_json(base/'messages.json', [])
    rows = e.read_json(dest/'messages.json', [])
    delta = e.read_json(dest/'new_messages.json', [])
    index = e.read_json(dest/'messages_by_id.json', {})
    assert rows == lines(dest/'messages.jsonl')
    assert delta == lines(dest/'new_messages.jsonl')
    assert {r['message_id']:r for r in rows} == index
    assert len(index) == len(rows)
    old_ids = {r['message_id'] for r in before}
    assert {r['message_id'] for r in delta} == set(index)-old_ids
    assert len(delta) == len({r['message_id'] for r in delta})
    start,end = (e.parse_time(manifest[k]) for k in ('start','end'))
    assert all(start <= e.parse_time(r['create_time']) <= end for r in delta)
    state = e.read_json(dest/'sync_state.json', {})
    assert state['status'] == 'completed' and state['message_pagination_complete']
    progress = e.read_json(dest/'thread_progress.json', {})
    expected_threads = {r['thread_id'] for r in rows if r.get('thread_id')}
    assert expected_threads <= set(progress['completed'])
    allowed_roots = {dest/'attachments'}
    ancestor = base
    seen = set()
    while ancestor not in seen:
        seen.add(ancestor)
        allowed_roots.add(ancestor/'attachments')
        previous = e.read_json(ancestor/'increment_manifest.json', {})
        if not previous.get('base_export'):
            break
        ancestor = Path(previous['base_export']).resolve()
    failures, checked, new_checked = [],0,0
    for row in rows:
        for item in row.get('attachments', []):
            if item.get('download_status') != 'downloaded':
                failures.append({'message_id':row['message_id'],'name':item.get('file_name'),
                                 'status':item.get('download_status'),'new':row['message_id'] not in old_ids})
                continue
            path = (dest/item['storage_path']).resolve()
            assert any(path.is_relative_to(root.resolve()) for root in allowed_roots), path
            assert path.is_file() and e.sha256_file(path) == item['sha256'], path
            checked += 1
            new_checked += row['message_id'] not in old_ids
    audit = lines(output/'audit.jsonl')
    assert [x['message_id'] for x in audit] == [x['message_id'] for x in delta]
    source_map = {}
    for path in old_audits:
        for r in lines(path):
            source_map[r['message_id']] = r['source_id']
    def normalize(t):
        return re.sub(r'\s+',' ',semantic_text(t)).strip()
    old_text = {}
    for r in before:
        t = normalize(redact(r.get('current_content'),r.get('mentions') or []))
        if len(t) >= 55:
            old_text.setdefault(t,[]).append(source_map.get(r['message_id'],r['message_id']))
    reposts = []
    for r in audit:
        t = normalize(r['text'])
        if len(t)<55:
            continue
        if t in old_text:
            reposts.append({'source_id':r['source_id'],'kind':'exact','old_sources':old_text[t]})
        else:
            matches = [sid for old,ids in old_text.items() if len(old)>=100 and old in t for sid in ids]
            if matches:
                reposts.append({'source_id':r['source_id'],'kind':'contains_old_text','old_sources':matches})
    e.write_json(output/'cross_batch_reposts.json',reposts)
    report = {'checks_passed':True,'base_sha256_unchanged':manifest['base_sha256'],
              'start':manifest['start'],'end':manifest['end'],'new_messages':len(delta),'merged_messages':len(rows),
              'latest_message_time':max(rows,key=e.sort_key)['create_time'],'completed_threads':len(expected_threads),
              'attachment_hashes_verified':checked,'new_attachment_hashes_verified':new_checked,
              'missing_attachments':failures,'cross_batch_reposts':len(reposts),
              'existing_semantic_updates':len(e.read_json(dest/'updated_existing_messages.json',[])),
              'timestamp_precision':'Exported message display times are minute-granular; API bounds use UTC seconds.',
              'limits':'Hash/serialization checks verify local integrity, not visibility of deleted or inaccessible server data. Content claims and linked documents are not independently validated.'}
    e.write_json(output/'verification.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--export',required=True)
    p.add_argument('--output',required=True)
    p.add_argument('--old-audit',action='append',default=[])
    args=p.parse_args()
    verify(args.export,args.output,args.old_audit)
