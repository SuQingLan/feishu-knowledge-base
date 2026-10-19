"""Import existing reviewed Feishu experience and its local evidence, offline."""
import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nebula.dataset import normalize
from nebula.store import Store

TOPICS = [('authoring', '出题', 'design'), ('grading', '评分', 'quality'),
          ('coding', 'Coding', 'engineering'), ('workflow', '流程', 'practice'),
          ('materials', '材料', 'quality'), ('troubleshooting', '排障', 'engineering'),
          ('material-grading', '材料与评分', 'quality'), ('excel', 'Excel验收', 'quality')]


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def words(value):
    if isinstance(value, list):
        return '\n'.join('• ' + words(item) for item in value)
    return str(value or '')


def iso(value):
    value = str(value)
    if len(value) == 10:
        return value + 'T00:00:00+08:00'
    if len(value) == 16:
        return value.replace(' ', 'T') + ':00+08:00'
    return value


def sections(item):
    fields = [('trigger', '适用场景'), ('current_assessment', '最新判断'),
              ('observation', '观察'), ('finding', '补充发现'), ('method', '可用做法'),
              ('limitation', '适用边界'), ('status', '核验状态'), ('evidence_kind', '证据类型')]
    result = [{'heading': label, 'text': words(item[key])} for key, label in fields if item.get(key)]
    result.append({'heading': '记录与验证', 'text': '记录日期：' + item['recorded'] +
                   '；独立验证：' + words(item.get('last_independent_verification') or '未独立复验，保留原资料判断')})
    for s in item.get('supplements', []):
        body = '\n\n'.join(x['heading'] + '：' + x['text'] for x in sections(s))
        result.append({'heading': s['recorded'] + ' · ' + s['title'], 'text': body})
    return result


def body(parts):
    return '\n\n'.join(x['heading'] + '\n' + x['text'] for x in parts)


def build(exporter, batch):
    reviewed = exporter / 'cleaned' / batch
    export = exporter / 'exports' / ('increment-' + batch)
    verification = read(reviewed / 'verification.json')
    if not verification['checks_passed']:
        raise ValueError('Source batch verification did not pass')
    cards = read(reviewed / 'knowledge_cards_merged.json')
    manifest = read(reviewed / 'markdown/manifest.json')
    messages = {}
    with (export / 'messages.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            message = json.loads(line)
            mid = message['message_id']
            if mid in messages:
                raise ValueError('Duplicate message: ' + mid)
            messages[mid] = message
    if len(messages) != verification['merged_messages']:
        raise ValueError('Message count disagrees with verified snapshot')
    sources = {}
    groups = {}
    for mid, m in messages.items():
        group = m['chat_id']
        groups[group] = {'id': group, 'name': '飞书群聊原始记录'}
        text = m.get('current_content') or m.get('content') or ''
        attachment_names = [a.get('file_name') or a.get('name') or '图片或附件' for a in m.get('attachments', [])]
        if attachment_names:
            text += '\n\n附件索引：' + '、'.join(attachment_names)
        sid = 'message:' + mid
        sources[sid] = {'id': sid, 'title': (text.splitlines()[0][:120] if text else '非文字消息') or '非文字消息',
                        'body': text, 'chat': group, 'sender': m.get('sender_name') or '群成员（未提供姓名）',
                        'time': iso(m['create_time']), 'url': m.get('message_app_link') or '',
                        'kind': 'topic' if m.get('thread_id') else 'fragment', 'thread_id': m.get('thread_id'),
                        'withdrawn': bool(m.get('deleted') or m.get('is_recalled'))}
    groups['reviewed'] = {'id': 'reviewed', 'name': '已整理经验 · ' + batch}
    groups['attachments'] = {'id': 'attachments', 'name': 'Markdown附件全文'}
    doc_ids = set()
    for doc in manifest['documents']:
        original = Path(doc['path'])
        if hashlib.sha256(original.read_bytes()).hexdigest() != doc['sha256']:
            raise ValueError('Attachment hash mismatch: ' + doc['document_id'])
        sid = doc['document_id']
        doc_ids.add(sid)
        occurrence = next(o for o in manifest['occurrences'] if o.get('document_id') == sid)
        sources[sid] = {'id': sid, 'title': doc['filename'], 'body': original.read_text(encoding=doc['encoding']),
                        'chat': 'attachments', 'sender': '附件作者（历史资料）', 'time': iso(occurrence['time']),
                        'url': sources['message:' + occurrence['message_id']]['url'], 'kind': 'document',
                        'sha256': doc['sha256'], 'local_path': str(original)}

    def refs(item):
        result = ['message:' + s['message_id'] for s in item.get('sources', [])]
        if item.get('document_id'):
            result.append(item['document_id'])
        for supplement in item.get('supplements', []):
            result.extend(refs(supplement))
        if any(s not in sources for s in result):
            raise ValueError('Missing evidence: ' + repr(set(result) - sources.keys()))
        return sorted(set(result))

    topic_ids = {name: key for key, name, _ in TOPICS}
    summaries, atoms, supplements = [], [], {}
    for c in cards:
        parts = sections(c)
        text = body(parts)
        sid = 'curation:' + c['id']
        sources[sid] = {'id': sid, 'title': c['id'] + ' · ' + c['title'], 'body': text, 'chat': 'reviewed',
                        'sender': '本地经验整理', 'time': iso(max([c['recorded']] + [s['recorded'] for s in c.get('supplements', [])])),
                        'url': '', 'kind': 'document', 'local_path': str(reviewed / 'knowledge_cards_merged.json')}
        evidence = refs(c) + [sid]
        summary = words(c.get('current_assessment') or c['method'])
        common = {'id': c['id'], 'title': c['title'], 'summary': summary, 'topic': topic_ids[c['topic']],
                  'source_ids': evidence, 'origin': 'local_reviewed_experience'}
        summaries.append(dict(common, sections=parts))
        atoms.append(dict(common, body=text, kind='experience', importance=80, status='rule_extract',
                          created_at=iso(c['recorded']), boundary=words(c['limitation']) + '；' + c['status']))
        for s in c.get('supplements', []):
            key = s.get('document_id') or s.get('id') or hashlib.sha256(s['title'].encode()).hexdigest()[:20]
            if key not in supplements:
                supplements[key] = {'item': s, 'refs': set(), 'parents': [], 'topic': topic_ids[c['topic']]}
            entry = supplements[key]
            entry['refs'].update(refs(s))
            entry['parents'].append(c['id'])
    for key, entry in supplements.items():
        s = entry['item']
        text = body(sections(s)) + '\n\n关联经验卡：' + '、'.join(sorted(set(entry['parents'])))
        sid = 'supplement:' + key
        sources[sid] = {'id': sid, 'title': s['title'], 'body': text, 'chat': 'reviewed', 'sender': '本地经验整理',
                        'time': iso(s['recorded']), 'url': '', 'kind': 'document'}
        atoms.append({'id': sid, 'title': s['title'], 'summary': words(s.get('observation') or s.get('finding') or s.get('method'))[:900],
                      'body': text, 'topic': entry['topic'], 'source_ids': sorted(entry['refs']) + [sid],
                      'kind': 'resource' if s.get('document_id') else 'experience', 'importance': 65,
                      'origin': 'local_reviewed_supplement', 'status': 'rule_extract', 'created_at': iso(s['recorded']),
                      'boundary': words(s['limitation'])})
    dataset = {'version': 1, 'label': '飞书群聊经验库 · ' + batch, 'demo': False, 'groups': list(groups.values()),
               'categories': [{'id': key, 'name': name, 'color': color} for key, name, color in
                              [('design', '出题与业务判断', '#79dedc'), ('quality', '评分与交付验收', '#eccb85'),
                               ('engineering', 'Coding与工具排障', '#82bcef'), ('practice', '流程与经验复用', '#ada2ef')]],
               'topics': [{'id': key, 'name': name, 'category': category} for key, name, category in TOPICS],
               'sources': list(sources.values()), 'atoms': atoms, 'cards': summaries, 'paths': []}
    normalize(copy.deepcopy(dataset))
    receipt = {'batch': batch, 'messages': len(messages), 'experience_cards': len(cards), 'knowledge_stars': len(atoms),
               'deduplicated_supplements': len(supplements), 'markdown_documents': len(doc_ids),
               'sources': len(sources), 'missing_evidence': 0, 'source_verification': verification,
               'inputs': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                          [reviewed/'knowledge_cards_merged.json', reviewed/'markdown/manifest.json', export/'messages.jsonl']}}
    return dataset, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exporter', type=Path, default=Path(__file__).resolve().parents[2] / 'exporter')
    parser.add_argument('--batch', help='Reviewed batch date; default latest merged cards')
    parser.add_argument('--output', type=Path, default=Path('data/feishu-experience.json'))
    parser.add_argument('--import-db', type=Path)
    args = parser.parse_args()
    batch = args.batch or sorted(p.parent.name for p in (args.exporter/'cleaned').glob('*/knowledge_cards_merged.json'))[-1]
    dataset, receipt = build(args.exporter.resolve(), batch)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix('.tmp')
    temp.write_text(json.dumps(dataset, ensure_ascii=False), encoding='utf-8')
    temp.replace(args.output)
    if args.import_db:
        receipt['import'] = Store(args.import_db).import_data(copy.deepcopy(dataset), replace=True)
    args.output.with_suffix('.receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in receipt.items() if k not in ('inputs', 'source_verification')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
