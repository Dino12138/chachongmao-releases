#!/usr/bin/env python3
"""微信文件查重：找出内容完全相同的文件，把多余的副本移到废纸篓（可在废纸篓里放回）。

只监听 127.0.0.1，并用随机口令保护。
"""
import collections
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import webbrowser

VERSION = '1.0.6'
RES = Path(os.environ.get('DEDUP_RES') or Path(__file__).resolve().parent)
SHELL = int(os.environ.get('DEDUP_SHELL', '1'))
REPO = 'Dino12138/chachongmao-releases'
HOT = Path.home()/'Library/Application Support/CD的查重猫/hot'
# 先试 GitHub，连不上再走国内镜像
FEEDS = ([os.environ['DEDUP_FEED']] if os.environ.get('DEDUP_FEED') else []) + [f'https://raw.githubusercontent.com/{REPO}/main/update.json'] + \
        [f'https://{h}.jsdelivr.net/gh/{REPO}@main/update.json' for h in ('gcore', 'fastly', 'cdn')]
HOME = Path.home()
TRASH = HOME/'.Trash'
TOKEN = os.environ.get('DEDUP_TOKEN') or secrets.token_urlsafe(16)
WX = HOME/'Library/Containers/com.tencent.xinWeChat/Data'


def wechat_dirs():
    out = []
    for acc in (WX/'Documents/xwechat_files').glob('*/msg'):
        out += [p for p in (acc/'file', acc/'video') if p.is_dir()]
    old = WX/'Library/Application Support/com.tencent.xinWeChat'  # 旧版微信
    out += [p for p in old.glob('*/*/Message/MessageTemp') if p.is_dir()]
    return out


def wechat_blocked():
    try:
        os.listdir(WX/'Documents')
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def default_roots():
    return [dict(path=str(p), kind='wechat') for p in wechat_dirs()] + \
           [dict(path=str(HOME/'Downloads'), kind='downloads'), dict(path=str(HOME/'Desktop'), kind='desktop'),
            dict(path=str(HOME/'Documents'), kind='documents')]


# 保留优先级：桌面、文稿里整理过的 > 微信里 > 下载；桌面、文稿里的多份副本不自动勾选，交给用户决定
RANK = dict(desktop=0, documents=1, wechat=2, downloads=3, other=4)
ORGANIZED = ('desktop', 'documents')
state = dict(status='idle', done=0, total=0, groups=[], merged=0, error='')
stop = threading.Event()
MERGED = HOT.parent/'merged.json'  # 合并过的副本：路径 → [大小, 修改时间]，再次扫描时不再当成可清理的重复


class Stopped(Exception):
    pass


def load_merged():
    try:
        return json.loads(MERGED.read_text())
    except (OSError, ValueError):
        return {}


def save_merged(m):
    MERGED.parent.mkdir(parents=True, exist_ok=True)
    MERGED.write_text(json.dumps(m, ensure_ascii=False))


def kind_of(path, roots):
    for r in roots:
        if path.startswith(r['path'].rstrip('/') + '/'):
            return r['kind']
    return 'other'


def digest(p, head=False):
    m = hashlib.blake2b()
    with open(p, 'rb') as f:
        if head:
            m.update(f.read(65536))
        else:
            for b in iter(lambda: f.read(1 << 20), b''):
                m.update(b)
    return m.hexdigest()


def scan(roots, min_size):
    stop.clear()
    state.update(status='listing', done=0, total=0, groups=[], merged=0, error='')
    by_size = collections.defaultdict(list)
    for r in roots:
        for d, dirs, fs in os.walk(r['path']):
            dirs[:] = [x for x in dirs if not x.startswith('.') and not x.endswith(('.app', '.photoslibrary'))]
            if stop.is_set():
                raise Stopped
            for f in fs:
                if f.startswith('.'):
                    continue
                p = os.path.join(d, f)
                try:
                    s = os.lstat(p)
                except OSError:
                    continue
                if s.st_size >= min_size and os.path.isfile(p) and not os.path.islink(p):
                    by_size[s.st_size].append((p, s.st_mtime))
    cands = {k: v for k, v in by_size.items() if len(v) > 1}
    state.update(status='hashing', total=sum(len(v) for v in cands.values()))
    groups, merged = [], load_merged()
    for size, items in cands.items():
        if stop.is_set():
            raise Stopped
        buckets = collections.defaultdict(list)
        for p, mt in items:  # 先比开头 64KB，再对疑似重复的算整文件
            try:
                buckets[digest(p, True)].append((p, mt))
            except OSError:
                pass
        for same_head in buckets.values():
            full = collections.defaultdict(list)
            for p, mt in same_head:
                try:
                    full[digest(p) if len(same_head) > 1 else p].append((p, mt))
                except OSError:
                    pass
            for dup in full.values():
                if len(dup) > 1:
                    g = make_group(size, dup, roots, merged)
                    if g['waste']:
                        groups.append(g)
                    else:
                        state['merged'] += 1  # 这一组已经合并过，不占额外空间
            state['done'] += len(same_head)
    groups.sort(key=lambda g: -g['waste'])
    state.update(status='done', groups=groups)


def make_group(size, dup, roots, merged):
    files = [dict(path=p, name=os.path.basename(p), mtime=mt, kind=kind_of(p, roots),
                  merged=merged.get(p) == [size, int(mt)]) for p, mt in dup]
    # 带 (1)(2) 的通常是重复下载产生的，同一位置时优先保留原名、较早的那份
    files.sort(key=lambda f: (f['merged'], RANK[f['kind']], '(' in f['name'], f['mtime']))
    for i, f in enumerate(files):
        f['keep'] = i == 0
        f['pick'] = i > 0 and not f['merged'] and f['kind'] not in ORGANIZED
    return dict(size=size, files=files, waste=size*sum(1 for f in files[1:] if not f['merged']))


def prune():
    # 不重新比对内容，只按硬盘现状剔除：已删除、被移走、大小或修改时间变了的文件；只剩一份或已全部合并的组不再列出
    m = load_merged(); left = []
    for g in state['groups']:
        files = []
        for f in g['files']:
            try:
                st = os.stat(f['path'])
            except OSError:
                continue
            if st.st_size == g['size'] and abs(st.st_mtime - f['mtime']) < 2:
                f['merged'] = m.get(f['path']) == [g['size'], int(f['mtime'])]
                files.append(f)
        if len(files) < 2:
            continue
        if not any(f['keep'] for f in files):
            files[0]['keep'] = True
        for f in files:
            f['pick'] = f['pick'] and not f['keep'] and not f['merged']
        g['files'] = files
        g['waste'] = g['size']*sum(1 for f in files if not f['keep'] and not f['merged'])
        if g['waste']:
            left.append(g)
        else:
            state['merged'] = state.get('merged', 0) + 1
    state['groups'] = left


def clone_over(keep, path):
    # APFS 克隆：原位置的文件名和路径都不变，但和保留的那份共用同一块硬盘空间
    if os.path.getsize(keep) != os.path.getsize(path) or digest(keep) != digest(path):
        raise OSError('内容已变化')
    tmp = f'{path}.dedup-tmp'
    subprocess.run(['cp', '-c', '-p', keep, tmp], check=True)
    os.replace(tmp, path)
    m = load_merged(); st = os.stat(path)
    m[path] = [st.st_size, int(st.st_mtime)]; save_merged(m)


def fetch(url, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'chachongmao', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def ver(s):
    return tuple(int(x) for x in s.split('.'))


def check_update():
    for u in FEEDS:
        try:
            info = json.loads(fetch(u, 8))
            new = ver(info['version']) > ver(VERSION)
            return dict(ok=True, current=VERSION, latest=info['version'], notes=info.get('notes', ''), new=new,
                        need_dmg=new and info.get('min_shell', 1) > SHELL, page=f'https://github.com/{REPO}/releases/latest')
        except Exception:
            continue
    return dict(ok=False, current=VERSION)


def apply_update():
    """下载新程序文件，逐个核对 SHA-256，全部通过才替换；替换后原地重启，窗口自动刷新。"""
    info = None
    for u in FEEDS:
        try:
            info = json.loads(fetch(u, 8)); break
        except Exception:
            continue
    if not info or info.get('min_shell', 1) > SHELL:
        raise OSError('无法热更新')
    stage = HOT.parent/'hot.new'
    shutil.rmtree(stage, ignore_errors=True); stage.mkdir(parents=True)
    for name, f in info['files'].items():
        data = None
        for url in f['urls']:
            try:
                d = fetch(url, 60)
                if hashlib.sha256(d).hexdigest() == f['sha256']:
                    data = d; break
            except Exception:
                continue
        if data is None:
            raise OSError(f'{name} 下载或校验失败')
        (stage/name).write_bytes(data)
    (stage/'version.json').write_text(json.dumps(dict(version=info['version'], min_shell=info.get('min_shell', 1))))
    old = HOT.parent/'hot.old'
    shutil.rmtree(old, ignore_errors=True)
    if HOT.exists():
        HOT.rename(old)
    stage.rename(HOT)
    shutil.rmtree(old, ignore_errors=True)
    return info['version']


def restart():
    time.sleep(0.5)
    os.environ.update(DEDUP_TOKEN=TOKEN, DEDUP_PORT=str(PORT), DEDUP_NO_WINDOW='1')
    os.execv(sys.executable, [sys.executable] + sys.argv[1:] if getattr(sys, 'frozen', False) else [sys.executable] + sys.argv)


def to_trash(path):
    src = Path(path)
    dst = TRASH/src.name
    if dst.exists():
        dst = TRASH/f'{src.stem} {time.strftime("%H.%M.%S")}-{secrets.token_hex(2)}{src.suffix}'
    shutil.move(str(src), str(dst))


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):  # 跳过 getfqdn：代理环境下反查本机名会卡住启动
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype='application/json'):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/':
            return self.send((RES/'dedup.html').read_text().replace('__TOKEN__', TOKEN).encode(), ctype='text/html')
        if self.headers.get('X-Token') != TOKEN:
            return self.send({'error': 'forbidden'}, 403)
        if u.path == '/api/roots':
            return self.send(dict(roots=default_roots(), blocked=wechat_blocked()))
        if u.path == '/api/version':
            return self.send({'version': VERSION})
        if u.path == '/api/state':
            return self.send(state)
        self.send({'error': 'not found'}, 404)

    def do_POST(self):
        if self.headers.get('X-Token') != TOKEN:
            return self.send({'error': 'forbidden'}, 403)
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        path = urlparse(self.path).path
        if path == '/api/scan':
            if state['status'] in ('listing', 'hashing'):
                return self.send({'ok': False})
            def run():
                try:
                    scan(data['roots'], int(data.get('min_size', 1024)))
                except Stopped:
                    state.update(status='stopped')
                except Exception as e:
                    state.update(status='error', error=str(e))
            threading.Thread(target=run, daemon=True).start()
            return self.send({'ok': True})
        if path == '/api/stop':
            stop.set()
            return self.send({'ok': True})
        if path in ('/api/trash', '/api/clone'):
            items = data['items']  # [{path, keep}]
            touched = {it['path'] for it in items}
            where = {f['path']: g for g in state['groups'] for f in g['files']}
            done, failed, freed = [], [], 0
            for it in items:
                p, keep = it['path'], it['keep']
                g = where.get(p)
                if not g or where.get(keep) is not g or p == keep or keep in touched or not os.path.exists(keep):
                    failed.append(p); continue
                try:
                    clone_over(keep, p) if path == '/api/clone' else to_trash(p)
                    done.append(p); freed += g['size']
                except (OSError, subprocess.CalledProcessError):
                    failed.append(p)
            prune()
            return self.send({'done': len(done), 'failed': failed, 'freed': freed})
        if path == '/api/refresh':
            if state['status'] in ('listing', 'hashing'):
                return self.send({'ok': False})
            before = sum(len(g['files']) for g in state['groups'])
            prune()
            return self.send({'ok': True, 'gone': before - sum(len(g['files']) for g in state['groups'])})
        if path == '/api/check':
            return self.send(check_update())
        if path == '/api/update':
            try:
                v = apply_update()
            except Exception as e:
                return self.send({'ok': False, 'error': str(e)})
            threading.Thread(target=restart, daemon=True).start()
            return self.send({'ok': True, 'version': v})
        if path == '/api/open':
            subprocess.run(['open', data['url']]) if data.get('url', '').startswith('https://github.com/') else None
            return self.send({'ok': True})
        if path == '/api/reveal':
            subprocess.run(['open', '-R', data['path']])
            return self.send({'ok': True})
        if path == '/api/pick':
            out = subprocess.run(['osascript', '-e', 'POSIX path of (choose folder with prompt "选择要查重的文件夹")'],
                                 capture_output=True, text=True)
            return self.send({'path': out.stdout.strip().rstrip('/')})
        self.send({'error': 'not found'}, 404)


def main():
    global PORT
    srv = LocalServer(('127.0.0.1', int(os.environ.get('DEDUP_PORT', 0))), Handler)
    PORT = srv.server_port
    if os.environ.pop('DEDUP_NO_WINDOW', None):
        return srv.serve_forever()
    url = f'http://127.0.0.1:{srv.server_port}/'
    chrome = Path('/Applications/Google Chrome.app')
    if chrome.exists():
        subprocess.Popen(['open', '-na', str(chrome), '--args', f'--app={url}', '--window-size=1100,800',
                          f'--user-data-dir={HOME}/Library/Application Support/wechat-dedup/chrome'])
    else:
        webbrowser.open(url)
    srv.serve_forever()


if __name__ == '__main__':
    main()
