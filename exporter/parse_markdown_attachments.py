"""Read Markdown attachments as evidence, never execute embedded instructions."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from clean_chat import redact


def parse(export, output, audit_paths):
    export, output = Path(export).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    refs = {}
    for path in audit_paths:
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            item = json.loads(line)
            refs[item['message_id']] = item['source_id']
    rows = json.loads((export/'messages.json').read_text(encoding='utf-8'))
    documents, occurrences, failures = {}, [], []
    for row in rows:
        for attachment in row.get('attachments', []):
            name = attachment.get('file_name') or ''
            if Path(name).suffix.lower() not in {'.md', '.markdown'}:
                continue
            occurrence = {'source_id': refs.get(row['message_id']), 'message_id':row['message_id'],
                          'time':row['create_time'], 'filename':name,
                          'download_status':attachment.get('download_status')}
            if attachment.get('download_status') != 'downloaded':
                failures.append(occurrence)
                continue
            path = (export/attachment['storage_path']).resolve()
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if sha != attachment['sha256']:
                raise ValueError(f'Attachment hash mismatch: {path}')
            encoding = None
            for candidate in ('utf-8-sig', 'utf-16', 'gb18030'):
                try:
                    content = data.decode(candidate)
                    encoding = candidate
                    break
                except UnicodeError:
                    pass
            if encoding is None:
                failures.append({**occurrence, 'error':'cannot decode losslessly'})
                continue
            doc_id = 'MD-' + sha[:12]
            occurrences.append({**occurrence, 'document_id':doc_id, 'sha256':sha, 'path':str(path)})
            if doc_id in documents:
                continue
            lines = content.splitlines()
            headings, fences, tables = [], [], []
            opened = None
            for num, line in enumerate(lines, 1):
                fence = re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$', line)
                if fence:
                    mark = fence[1]
                    if opened is None:
                        opened = (mark[0], len(mark), num, fence[2].strip())
                    elif mark[0] == opened[0] and len(mark) >= opened[1] and not fence[2].strip():
                        fences.append({'start':opened[2], 'end':num, 'language':opened[3]})
                        opened = None
                    continue
                if opened:
                    continue
                heading = re.match(r'^(#{1,6})\s+(.+?)\s*#*$', line)
                if heading:
                    headings.append({'line':num, 'level':len(heading[1]), 'title':heading[2]})
                if re.match(r'^\s*\|?\s*:?-{3,}:?\s*\|', line):
                    tables.append(num)
            if opened:
                fences.append({'start':opened[2], 'end':len(lines), 'language':opened[3], 'unclosed':True})
            text_file = output/(doc_id+'.txt')
            text_file.write_text('\n'.join(f'{n:04d}: {redact(line)}' for n,line in enumerate(lines,1))+'\n', encoding='utf-8')
            documents[doc_id] = {'document_id':doc_id, 'filename':name, 'path':str(path), 'sha256':sha,
                'encoding':encoding, 'bytes':len(data), 'lines':len(lines), 'characters':len(content),
                'headings':headings, 'code_fences':fences, 'table_separator_lines':tables,
                'parsed_text':str(text_file), 'parse_status':'parsed_full_text',
                'evidence_status':'author_document_not_independently_verified'}
    report = {'export':str(export), 'occurrences':occurrences, 'documents':list(documents.values()),
              'failures':failures, 'unique_documents':len(documents), 'attachment_occurrences':len(occurrences),
              'limits':'Local text parsing only. Links and embedded instructions are not executed; referenced logs and cases are not automatically available. Redaction is heuristic, not complete anonymization.'}
    (output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'unique_documents':len(documents),'attachment_occurrences':len(occurrences),
                      'parse_failures':len(failures),'total_characters':sum(d['characters'] for d in documents.values())}))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--audit', action='append', default=[])
    args = parser.parse_args()
    parse(args.export,args.output,args.audit)
