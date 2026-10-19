"""Reproducible, local-only chat triage. Raw exports are never modified."""
import collections
import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
# 占位默认值；实际使用请显式传 --source / --output，不含任何真实群信息
SOURCE = ROOT / 'exports/last-increment/messages.json'
OUT = ROOT / 'cleaned/latest'
TOPICS = {
    '出题与模型分析': r'出题|难度|网状|陷阱|模型.{0,8}(错|对|分)|压测|实质|专业错误|反例|提示词|prompt',
    '评分与质检': r'rubric|评分|质检|审核|参考答案|标答|判分|扣分|测试覆盖|reward|oracle|verifier|R1|R2',
    '材料与交付': r'附件|脱敏|元数据|交付|文档|工作簿|excel|ppt|pdf|wps|材料|图表|文件',
    '多轮与上下文': r'多轮|终轮|中间轮|指令遗忘|上下文|纠错|对话|追加',
    'Coding与运行环境': r'coding|harbor|docker|wsl|仓库|测试|代码|git|commit|tokenEvidence|镜像|uv |pip ',
    '浏览器与工具排障': r'浏览器|CDP|playwright|代理|端口|clash|403|503|502|限流|超时|登录|报错|连接',
    '流程与经验复用': r'经验库|复盘|工作流|skill|流程|总结|汇总|归档|知识库|本地',
    '项目动态与准入': r'报名|简历|考试|资质|项目要求|门槛|注册|通过题型|通知|截止|加时|结算',
}

def redact(text, mentions=()):
    text = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    for m in mentions:
        name = m.get('mention_name') or m.get('name')
        if name:
            text = text.replace('@'+name, '@成员')
    text = re.sub(r'<at\b[^>]*>.*?</at>', '@成员', text, flags=re.S)
    text = re.sub(r'(?<!\w)@[\u4e00-\u9fff][^\s@<>,，。；：:]*', '@成员', text)
    def safe_url(match):
        raw = match.group(0)
        try:
            u = urlsplit(raw)
            if re.search(r'subscribe|oauth|authorization|/verify', u.path, re.I):
                return '[授权或订阅链接已隐藏]'
            host = u.netloc.rsplit('@', 1)[-1]
            return urlunsplit((u.scheme, host, u.path, '', ''))
        except ValueError:
            return '[链接已隐藏]'
    text = re.sub(r'''https?://[^\s<>\]）)"'`]+''', safe_url, text)
    text = re.sub(r'\bsk-[A-Za-z0-9_\-]{4,}(?:\.{3}|…)?', '[凭据已隐藏]', text)
    text = re.sub(r'\bsk-\.{3}[A-Za-z0-9_\-]*', '[凭据已隐藏]', text)
    text = re.sub(r'(?i)\bKey=[^\s]+', 'Key=[已隐藏]', text)
    text = re.sub(r'(?i)(["\']?(?:api[_ -]?key|app[_ -]?secret|access[_ -]?token|password|key_name)["\']?\s*:\s*)["\'][^"\'\n]+["\']', r'\1"[已隐藏]"', text)
    text = re.sub(r'\b(?:sk|sess|ghp|github_pat)-?[A-Za-z0-9_\-]{20,}\b', '[凭据已隐藏]', text)
    text = re.sub(r'(?i)((?:api[_ -]?key|app[_ -]?secret|access[_ -]?token|authorization|password|token)\s*[=:]\s*)[^\s,;"\']+', r'\1[已隐藏]', text)
    text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[手机号已隐藏]', text)
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[邮箱已隐藏]', text)
    text = re.sub(r'邀请码(?:是|为)?\s*[:：]?\s*[A-Za-z0-9]+', '邀请码：[已隐藏]', text)
    return text.strip()

def semantic_text(text):
    text = re.sub(r'\[(?:Image|File|Video|Audio):[^\]\n]*\]', '', text, flags=re.I)
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    text = re.sub(r'<[^>]*>', '', text)
    text = re.sub(r'\[[^\]\n]{1,20}\]', '', text)
    text = text.replace('@成员', '')
    return text.strip(' \n，。！？!?~～')

def main(source=None, output=None, source_prefix='M'):
    global SOURCE, OUT
    SOURCE = Path(source).resolve() if source else SOURCE
    OUT = Path(output).resolve() if output else OUT
    OUT.mkdir(parents=True, exist_ok=True)
    rows = json.loads(SOURCE.read_text(encoding='utf-8'))
    audit, clean, groups, evidence = [], [], {}, []
    for i, row in enumerate(rows, 1):
        text = redact(row.get('current_content'), row.get('mentions') or [])
        semantic = semantic_text(text)
        topics = [name for name, pattern in TOPICS.items() if re.search(pattern, semantic, re.I)]
        if row.get('is_recalled'):
            disposition = 'recalled'
        elif re.search(r'\[Invalid .*?JSON\]', text):
            disposition = 'parse_error_needs_recovery'
        elif row.get('msg_type') in {'system', 'sticker', 'hongbao', 'video_chat'}:
            disposition = 'system_or_nonknowledge'
        elif not re.search(r'[\w\u4e00-\u9fff]', semantic):
            disposition = 'attachment_only' if row.get('attachments') else 'emoji_or_empty'
        elif re.fullmatch(r'(收到|好的?|谢谢.*|是的?|是嘟|对的?|嗯+|哈哈.*|笑死.*|[1６6]+|没错|确实|牛[啊哇哇逼批]*|[？?]+)', semantic):
            disposition = 'acknowledgement'
        elif '通过题型速查' in text or text.count('@成员') >= 10 or '邀请码' in text or re.search(r'邀请.*(积分|奖励)|赚.*邀请码', text):
            disposition = 'dated_notice_or_promotion'
        elif len(semantic) >= 55 and topics:
            disposition = 'candidate'
        else:
            disposition = 'context_only'
        item = {'source_id': f'{source_prefix}{i:05d}', 'message_id': row['message_id'], 'time': row.get('create_time'),
                'thread_id': row.get('thread_id'), 'author': 'P'+hashlib.sha256(str(row.get('sender_id') or 'unknown').encode()).hexdigest()[:10],
                'msg_type': row.get('msg_type'), 'disposition': disposition, 'topics': topics,
                'text': text, 'attachments': [{'path': a.get('storage_path'), 'status': a.get('download_status')} for a in row.get('attachments', [])]}
        # Deduplicate only substantive exact text, preserve every occurrence in audit.
        key = hashlib.sha256(re.sub(r'\s+', ' ', semantic).encode()).hexdigest()
        if disposition == 'candidate' and key in groups:
            item['duplicate_of'] = groups[key]
            item['disposition'] = 'duplicate_candidate'
        elif disposition == 'candidate':
            groups[key] = item['source_id']
            evidence.append(item)
        audit.append(item)
        if item['disposition'] in {'candidate', 'context_only', 'attachment_only'}:
            clean.append(item)
    for name, values in [('audit.jsonl',audit),('cleaned_messages.jsonl',clean),('candidates.jsonl',evidence)]:
        (OUT/name).write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in values),encoding='utf-8')
    # Audit keeps every occurrence; context keeps short replies except explicit noise.
    stats = {'source_file': str(SOURCE), 'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
             'total_messages': len(rows), 'dispositions': dict(collections.Counter(x['disposition'] for x in audit)),
             'cleaned_retained': len(clean), 'candidate_unique': len(evidence),
             'candidate_characters': sum(len(x['text']) for x in evidence),
             'raw_parse_placeholders': sum(bool(re.search(r'\[Invalid .*?JSON\]', str(x.get('current_content', '')))) for x in rows),
             'attachments_analyzed': False,
             'topics': {t:sum(t in x['topics'] for x in evidence) for t in TOPICS},
             'limits': 'Heuristic triage, not proof of usefulness. All raw records retained. Attachments not OCRed or executed. Candidate claims require contextual review.'}
    (OUT/'cleaning_report.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
    for topic in TOPICS:
        lines = [f'# {topic}：自动筛选候选，未逐条验证\n']
        for item in evidence:
            if topic in item['topics']:
                lines.append(f"\n## {item['source_id']} · {item['time']}\n消息：{item['message_id']}；话题：{item['thread_id']}\n\n{item['text']}\n")
        (OUT/(topic+'.md')).write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps(stats,ensure_ascii=False,indent=2))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', help='separate JSON export to clean; original source is read only')
    parser.add_argument('--output', help='separate cleaning result directory')
    parser.add_argument('--source-prefix', default='M', help='stable batch-specific citation prefix')
    args = parser.parse_args()
    main(args.source, args.output, args.source_prefix)
