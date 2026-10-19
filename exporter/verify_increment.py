"""Verify a frozen increment, resource hashes, OCR coverage and cross-batch reposts."""
import collections
import hashlib
import json
import re
from pathlib import Path
import devinsync_feishu_chat_local as e
from clean_chat import redact, semantic_text

ROOT = Path(__file__).resolve().parent
DEST = ROOT / 'exports/increment-2026-10-09'
OUT = ROOT / 'cleaned/2026-10-09'

def read_lines(path):
    return [json.loads(s) for s in path.read_text(encoding='utf-8-sig').splitlines() if s.strip()]

def main():
    manifest = e.read_json(DEST/'increment_manifest.json', {})
    base = Path(manifest['base_export'])
    assert e.sha256_file(base/'messages.json') == manifest['base_sha256']
    before = e.read_json(base/'messages.json', [])
    rows = e.read_json(DEST/'messages.json', [])
    delta = e.read_json(DEST/'new_messages.json', [])
    index = e.read_json(DEST/'messages_by_id.json', {})
    assert rows == read_lines(DEST/'messages.jsonl')
    assert delta == read_lines(DEST/'new_messages.jsonl')
    assert {r['message_id']: r for r in rows} == index
    assert len(index) == len(rows)
    old_ids = {r['message_id'] for r in before}
    assert {r['message_id'] for r in delta} == set(index) - old_ids
    assert len(delta) == len({r['message_id'] for r in delta})
    start, end = (e.parse_time(manifest[k]) for k in ('start','end'))
    assert all(start <= e.parse_time(r['create_time']) <= end for r in delta)
    failures, checked = [], 0
    for r in rows:
        for a in r.get('attachments', []):
            if a.get('download_status') != 'downloaded':
                failures.append({'message_id':r['message_id'], 'status':a.get('download_status')})
                continue
            path = (DEST/a['storage_path']).resolve()
            assert any(path.is_relative_to(root.resolve()) for root in (DEST/'attachments', base/'attachments'))
            assert path.is_file() and e.sha256_file(path) == a['sha256']
            checked += 1
    audit = read_lines(OUT/'audit.jsonl')
    assert [x['message_id'] for x in audit] == [x['message_id'] for x in delta]
    def normalized(t):
        return re.sub(r'\s+', ' ', semantic_text(t)).strip()
    old_text = {}
    for i,r in enumerate(before,1):
        t = normalized(redact(r.get('current_content'),r.get('mentions') or []))
        if len(t)>=55: old_text.setdefault(t, []).append(f'M{i:05d}')
    reposts = []
    for item in audit:
        t = normalized(item['text'])
        if len(t)<55: continue
        if t in old_text:
            reposts.append({'source_id':item['source_id'],'kind':'exact','old_sources':old_text[t]})
        else:
            matches=[sid for old,sids in old_text.items() if len(old)>=100 and old in t for sid in sids]
            if matches: reposts.append({'source_id':item['source_id'],'kind':'contains_old_text','old_sources':matches})
    e.write_json(OUT/'cross_batch_reposts.json',reposts)
    ocr = [x for name in ('ocr_interim.jsonl','ocr_part2.jsonl') for x in read_lines(OUT/name)]
    unique_ocr = {str(Path(x['path']).resolve()):x for x in ocr}
    images = [(DEST/a['storage_path']).resolve() for r in delta for a in r.get('attachments',[]) if a.get('resource_type')=='image' and a.get('download_status')=='downloaded']
    assert set(map(str,images)) == set(unique_ocr)
    repaired = [x for x in read_lines(OUT/'ocr_repair.jsonl') if x.get('status')=='ocr_done']
    assert all(x['status']=='ocr_done' or (x['status']=='ocr_failed' and
               x['message_id']=='om_x100b63b8d8bcd4a8de296894a64d764' and len(repaired)==6)
               for x in unique_ocr.values())
    ocr_audit=[]
    by_id = {r['message_id']:r for r in audit}
    for path,x in unique_ocr.items():
        # OCR may contain names or imperfectly recognized credentials; keep local only.
        recognized = x.get('text','') if x['status']=='ocr_done' else '\n'.join(part['text'] for part in repaired)
        ocr_audit.append({'source_id':by_id[x['message_id']]['source_id'], 'message_id':x['message_id'],
                          'path':str(Path(path).relative_to(DEST)), 'sha256':e.sha256_file(Path(path)),
                          'text':redact(recognized), 'status':'ocr_done_not_full_manual_transcription',
                          'preprocessing':'six overlapping crops' if x['status']=='ocr_failed' else 'none'})
    e.write_jsonl(OUT/'ocr_audit.jsonl',ocr_audit)
    report = {'checks_passed':True,'base_sha256_unchanged':manifest['base_sha256'],
              'new_messages':len(delta),'merged_messages':len(rows),'start':manifest['start'],'end':manifest['end'],
              'latest_message_time':max(rows,key=e.sort_key)['create_time'],
              'attachment_hashes_verified':checked,'missing_attachments':failures,
              'new_images_ocr':len(images),'cross_batch_reposts':reposts,
              'existing_semantic_updates':len(e.read_json(DEST/'updated_existing_messages.json',[])),
              'limits':'All downloaded resource hashes checked. OCR is local and fallible. One video not transcribed; inaccessible image not recovered; recalled body unavailable. Older attachments not comprehensively analyzed.'}
    e.write_json(OUT/'verification.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
