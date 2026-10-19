"""Build an explicitly provisional view and OCR queue from saved checkpoints."""
import argparse
import json
from pathlib import Path
import devinsync_feishu_chat_local as e

def inspect(export, output):
    export,output=Path(export).resolve(),Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    manifest=e.read_json(export/'increment_manifest.json',{})
    base=e.read_json(Path(manifest['base_export'])/'messages_by_id.json',{})
    records={}
    for file in sorted((export/'checkpoints').glob('*.json')):
        for row in e.read_json(file,{}) .get('records',[]):
            if row['message_id'] not in base:records[row['message_id']]=row
    rows=sorted(records.values(),key=e.sort_key)
    e.write_json(output/'provisional_messages.json',rows)
    completed={}
    for file in output.glob('ocr_part_*.jsonl'):
        for line in file.read_text(encoding='utf-8-sig').splitlines():
            if line.strip():
                row=json.loads(line)
                completed[row['path']]=row
    queue=[{'message_id':r['message_id'],'path':str((export/a['storage_path']).resolve())}
           for r in rows for a in r.get('attachments',[])
           if a.get('resource_type')=='image' and a.get('download_status')=='downloaded'
           and str((export/a['storage_path']).resolve()) not in completed]
    e.write_json(output/'ocr_queue.json',queue)
    print(json.dumps({'provisional_messages':len(rows),'remaining_images':len(queue),
        'completed_threads':len(e.read_json(export/'thread_progress.json',{}).get('completed',[]))},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    inspect(args.export,args.output)
