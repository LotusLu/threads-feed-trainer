"""Local deterministic content rules, bounded history and shared viewport extraction."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

HOSTS = {'threads.com', 'www.threads.com', 'threads.net', 'www.threads.net'}

def canonical_post(url: str, base: str = 'https://www.threads.com/') -> tuple[str, str] | None:
    try:
        p = urlparse(urljoin(base, url))
        if p.scheme not in {'https', 'http'} or p.hostname not in HOSTS or p.username or p.password or p.port not in {None, 80, 443}:
            return None
        m = re.fullmatch(r'/(@[^/\s?#]+)/post/([A-Za-z0-9_-]+)/?', p.path)
        if not m:
            return None
        return m[2], f'{p.scheme}://{p.hostname}/{m[1]}/post/{m[2]}'
    except (ValueError, TypeError, AttributeError):
        return None

def normalize(text: str) -> str:
    return unicodedata.normalize('NFKC', text).casefold()

def keywords(text: str) -> list[str]:
    return list(dict.fromkeys(normalize(line.strip()) for line in text.splitlines() if line.strip()))

def contains(text: str, term: str) -> bool:
    # ASCII letters/digits at either end require ASCII word boundaries.
    left = r'(?<![a-z0-9_])' if term and term[0].isascii() and term[0].isalnum() else ''
    right = r'(?![a-z0-9_])' if term and term[-1].isascii() and term[-1].isalnum() else ''
    return bool(re.search(left + re.escape(term) + right, text))

def classify(text: str, includes: list[str], excludes: list[str], feedback: str | None = None) -> str:
    value = normalize(text).strip()
    if not value:
        return 'unknown'
    if any(contains(value, normalize(term)) for term in excludes):
        return 'excluded'
    if feedback == 'negative':
        return 'excluded'
    if feedback == 'positive' or any(contains(value, normalize(term)) for term in includes):
        return 'matched'
    return 'unmatched'

def fingerprint(includes: list[str], excludes: list[str]) -> str:
    return hashlib.sha256(json.dumps([sorted(map(normalize, includes)), sorted(map(normalize, excludes))], ensure_ascii=False).encode()).hexdigest()[:16]

def compare_reports(before: dict | None, after: dict | None) -> float | None:
    if not before or not after:
        return None
    if not all(r.get('verifiedForYou') and r.get('classifiable', 0) >= 5 and r.get('rate') is not None and r.get('status') in {'complete', 'partial'} for r in (before, after)):
        return None
    if not before.get('runId') or before.get('runId') != after.get('runId') or before.get('fingerprint') != after.get('fingerprint'):
        return None
    return round(after['rate'] - before['rate'], 1)

def validate_report(report: object) -> None:
    if not isinstance(report, dict):
        raise ValueError('invalid report')
    for key in ('runId', 'fingerprint', 'time', 'phase'):
        if not isinstance(report.get(key), str) or not 1 <= len(report[key]) <= 64:
            raise ValueError('invalid report metadata')
    if report['phase'] not in {'before', 'after'} or report.get('status') not in {'complete', 'partial', 'unavailable', 'stopped'} or not isinstance(report.get('verifiedForYou'), bool):
        raise ValueError('invalid report status')
    for key in ('sampled', 'classifiable', 'matched', 'unknown', 'ads', 'overlap', 'target'):
        if type(report.get(key)) is not int or not 0 <= report[key] <= (50 if key in {'sampled','classifiable','matched','unknown','target'} else 10000):
            raise ValueError('invalid report count')
    if report['matched'] > report['classifiable'] or report['classifiable'] + report['unknown'] != report['sampled'] or not 1 <= report['target'] <= 50:
        raise ValueError('inconsistent report counts')
    for key in ('rate','delta'):
        value = report.get(key)
        if value is not None and (type(value) not in {int,float} or not -100 <= value <= 100):
            raise ValueError('invalid report rate')
    ids = report.get('ids')
    if not isinstance(ids, list) or len(ids) != report['sampled'] or len(set(ids)) != len(ids) or any(not isinstance(item,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',item) for item in ids):
        raise ValueError('invalid report ids')
    if 'reason' in report and (not isinstance(report['reason'],str) or len(report['reason']) > 500):
        raise ValueError('invalid report reason')

class ContentStore:
    """Fail closed on corrupt history; never replace unreadable evidence."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = {'version': 1, 'posts': {}, 'reports': {}}
        self.error = None
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('posts'), dict) or not isinstance(data.get('reports'), dict):
                    raise ValueError('invalid history format')
                if len(data['posts']) > 5000:
                    raise ValueError('history too large')
                for post_id, entry in data['posts'].items():
                    if not isinstance(entry, dict) or not canonical_post(entry.get('url')) or canonical_post(entry['url'])[0] != post_id or entry.get('feedback') not in {None, 'positive', 'negative'} or entry.get('id') != post_id or not isinstance(entry.get('readAt'), str) or len(entry['readAt']) > 64:
                        raise ValueError('invalid history entry')
                for phase, report in data['reports'].items():
                    if phase not in {'before', 'after'}:
                        raise ValueError('invalid report phase')
                    validate_report(report)
                self.data = data
        except (ValueError, TypeError, OSError) as exc:
            self.error = f'無法讀取本機閱讀紀錄；保留原檔並停用紀錄寫入：{exc}'

    def snapshot(self) -> dict:
        with self.lock:
            recent = sorted(self.data['posts'].values(), key=lambda v: v['readAt'], reverse=True)[:12]
            quality = {label: sum(item.get('feedback') == label for item in self.data['posts'].values()) for label in ('positive', 'negative')}
            return {'qualityLabels': quality, 'recentReads': copy.deepcopy(recent), 'reports': copy.deepcopy(self.data['reports']), 'historyError': self.error}

    def entry(self, post_id: str) -> dict:
        with self.lock:
            return dict(self.data['posts'].get(post_id, {}))

    def _save(self, data: dict) -> None:
        if self.error:
            raise OSError(self.error)
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                os.fchmod(out.fileno(), 0o600)
                json.dump(data, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
            temporary = None
            self.data = data
        finally:
            if temporary:
                os.unlink(temporary)

    def read(self, url: str) -> None:
        canonical = canonical_post(url)
        if not canonical:
            raise ValueError('invalid post URL')
        post_id, url = canonical
        with self.lock:
            data = copy.deepcopy(self.data)
            data['posts'][post_id] = {**data['posts'].get(post_id, {}), 'id': post_id, 'url': url, 'readAt': datetime.now(timezone.utc).isoformat()}
            if len(data['posts']) > 5000:
                oldest = min(data['posts'], key=lambda k: data['posts'][k]['readAt'])
                del data['posts'][oldest]
            self._save(data)

    def feedback(self, post_id: object, value: object) -> None:
        if not isinstance(post_id, str) or value not in {'positive', 'negative', 'clear'}:
            raise ValueError('回饋格式錯誤。')
        with self.lock:
            if post_id not in self.data['posts']:
                raise ValueError('找不到已讀貼文。')
            data = copy.deepcopy(self.data)
            data['posts'][post_id]['feedback'] = None if value == 'clear' else value
            self._save(data)

    def start_run(self) -> None:
        with self.lock:
            data = copy.deepcopy(self.data)
            data['reports'] = {}
            self._save(data)

    def report(self, phase: str, report: dict) -> None:
        validate_report(report)
        if phase not in {'before', 'after'} or report['phase'] != phase:
            raise ValueError('invalid phase')
        with self.lock:
            data = copy.deepcopy(self.data)
            data['reports'][phase] = copy.deepcopy(report)
            self._save(data)

# Returns only visible, isolated cards. No body-text fallback. Multiple post IDs in
# a container are ambiguous: descend to smaller permalink ancestors or skip it.
EXTRACT_POSTS_JS = r'''() => {
 const visible = el => {const r=el.getBoundingClientRect(); const s=getComputedStyle(el); return r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth && s.visibility!=='hidden' && s.display!=='none';};
 const valid = a => {try {const u=new URL(a.getAttribute('href'),location.href);return ['threads.com','www.threads.com','threads.net','www.threads.net'].includes(u.hostname)&&!u.username&&!u.password&&/^\/@[^/\s]+\/post\/[A-Za-z0-9_-]+\/?$/.test(u.pathname)&&['http:','https:'].includes(u.protocol);}catch{return false;}};
 const links = el => [...el.querySelectorAll('a[href]')].filter(valid);
 const id = a => new URL(a.href).pathname.split('/post/')[1].replace(/\/$/,'');
 const roots = new Set();
 for(const a of [...document.querySelectorAll('a[href]')].filter(valid)) {
   if(!visible(a)) continue;
   let root=a.closest('article,[role="article"],[data-pressable-container]');
   if(root && new Set(links(root).map(id)).size===1) roots.add(root);
   else {let p=a.parentElement; for(let i=0;p&&i<7;i++,p=p.parentElement){
     if(p.matches('body,main,nav,header,[role="navigation"],[role="menu"]')) break;
     if(new Set(links(p).map(id)).size!==1) break;
     // Permalink/time plus a separate text block is the conservative fallback.
     if(p.querySelector('time') && p.querySelector('[data-post-text], [dir="auto"]')) {roots.add(p);break;}
   }}
 }
 const out=[]; const seen=new Set();
 for(const root of roots){
   if(!visible(root)||root.closest('nav,[role="navigation"],[role="menu"]'))continue;
   const ls=links(root); if(new Set(ls.map(id)).size!==1) continue;
   const a=ls.find(visible); if(!a||seen.has(id(a)))continue;
   const clone=root.cloneNode(true);
   clone.querySelectorAll('nav,header,[role="menu"],button,[role="button"],time,article article,[role="article"] [role="article"]').forEach(el=>el.remove());
   clone.querySelectorAll('a').forEach(el=>{if(valid(el) || !el.closest('[data-post-text],[dir="auto"]'))el.remove();else el.replaceWith(...el.childNodes);});
   const blocks=[...clone.querySelectorAll('[data-post-text], [dir="auto"]')].filter(el=>!el.parentElement.closest('[data-post-text], [dir="auto"]'));
   // Dedicated text blocks are required; outer container text may include replies or navigation.
   const text=blocks.map(el=>el.textContent.trim()).filter(Boolean).join('\n').slice(0,20000);
   const sponsored=[...root.querySelectorAll('[aria-label],span')].some(el=>!el.closest('[data-post-text],[dir="auto"],a') && /^(Sponsored|廣告|赞助|贊助)$/i.test((el.getAttribute('aria-label')||el.textContent||'').trim()));
   seen.add(id(a));out.push({url:a.href,text,sponsored});
 }
 return out;
}'''
