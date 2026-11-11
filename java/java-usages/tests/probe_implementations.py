"""Latency of the JDT LS requests behind Go to Implementation and the inheritance gutter
markers, on a real Maven project (#2693). Measurement only, not a test.
Arguments: jdtls home, project directory, output JSON path."""
import concurrent.futures
import json
import os
import pathlib
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time

home, project, out = (pathlib.Path(a).resolve() for a in sys.argv[1:4])
bundle = pathlib.Path(__file__).resolve().parents[1] / 'openidea-java-usages.jar'
work = pathlib.Path(tempfile.mkdtemp(prefix='openidea-probe-'))
config = work / 'config'
config.mkdir()
system, machine = platform.system(), platform.machine().lower()
config_name = {'Windows': 'config_win', 'Darwin': 'config_mac'}.get(system, 'config_linux')
config_names = ([config_name + '_arm'] if machine in ('arm64', 'aarch64') else []) + [config_name]
config_source = next(home / n / 'config.ini' for n in config_names if (home / n / 'config.ini').is_file())
(config / 'config.ini').write_bytes(config_source.read_bytes())

log = out.with_suffix('.stderr.log').open('w')
launcher = next((home / 'plugins').glob('org.eclipse.equinox.launcher_*.jar'))
# The app's defaults (-Xmx2G, G1); no Lombok agent.
proc = subprocess.Popen(['java', '-Declipse.application=org.eclipse.jdt.ls.core.id1', '-Dosgi.bundles.defaultStartLevel=4',
                         '-Declipse.product=org.eclipse.jdt.ls.core.product', '-Xmx2G', '-XX:+UseG1GC', '--add-modules=ALL-SYSTEM',
                         '--add-opens', 'java.base/java.util=ALL-UNNAMED', '--add-opens', 'java.base/java.lang=ALL-UNNAMED',
                         '-jar', str(launcher), '-configuration', str(config), '-data', str(work / 'metadata')],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
write_lock, id_lock = threading.Lock(), threading.Lock()
pending, statuses = {}, []
seq = 0
t_start = time.monotonic()


def send(message):
    data = json.dumps(message).encode()
    with write_lock:
        proc.stdin.write(f'Content-Length: {len(data)}\r\n\r\n'.encode() + data)
        proc.stdin.flush()


def reader():
    while True:
        headers = {}
        while True:
            line = proc.stdout.readline()
            if not line:
                return
            if line == b'\r\n':
                break
            k, v = line.decode().split(':', 1)
            headers[k.lower()] = v.strip()
        msg = json.loads(proc.stdout.read(int(headers['content-length'])))
        if 'method' in msg and 'id' in msg:
            items = (msg.get('params') or {}).get('items') or []
            result = [None] * len(items) if msg['method'] == 'workspace/configuration' else None
            send({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})
        elif 'id' in msg:
            slot = pending.pop(msg['id'], None)
            if slot:
                slot[1] = msg
                slot[0].set()
        elif msg.get('method') == 'language/status':
            statuses.append((time.monotonic() - t_start, msg['params']))


threading.Thread(target=reader, daemon=True).start()


def timed(method, params, timeout=300):
    """{'ok', 'ms', 'result' | 'error'}; never raises."""
    global seq
    with id_lock:
        seq += 1
        rid = seq
        slot = pending[rid] = [threading.Event(), None]
    t0 = time.monotonic()
    send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
    if not slot[0].wait(timeout):
        pending.pop(rid, None)
        return {'ok': False, 'ms': round((time.monotonic() - t0) * 1000), 'error': 'timeout'}
    ms = round((time.monotonic() - t0) * 1000)
    if 'error' in slot[1]:
        return {'ok': False, 'ms': ms, 'error': slot[1]['error'].get('message')}
    return {'ok': True, 'ms': ms, 'result': slot[1].get('result')}


def wait_status(kind, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(p.get('type') == kind for _, p in statuses):
            return True
        time.sleep(0.5)
    return False


KEYWORDS = {'return', 'new', 'throw', 'else', 'case', 'if', 'while', 'for', 'switch', 'catch', 'do', 'try', 'assert'}
TYPE_DECL = re.compile(r'^(?:public\s+)?(abstract\s+)?(?:final\s+)?(class|interface)\s+(\w+)')
METHOD = re.compile(r'^\s+(?:(?:public|protected|abstract|default|synchronized)\s+)*(?:<[^>]+>\s+)?([\w.$<>\[\], ?]+?)\s+(\w+)\s*\(')


def scan():
    """Per source file: (path, text, kind of its first type, type position, method positions)."""
    found = []
    for path in sorted(project.rglob('*.java')):
        rel = path.relative_to(project).as_posix()
        if '/src/main/java/' not in '/' + rel or '/target/' in '/' + rel:
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        lines = text.splitlines()
        decl = next(((i, m) for i, l in enumerate(lines) if (m := TYPE_DECL.match(l))), None)
        if not decl:
            continue
        line, m = decl
        kind = 'interface' if m.group(2) == 'interface' else 'abstract' if m.group(1) else 'class'
        methods = []
        for i, l in enumerate(lines[line + 1:], line + 1):
            mm = METHOD.match(l)
            if not mm or mm.group(1).split()[0] in KEYWORDS or re.search(r'\b(static|private|final)\b', l[:mm.start(2)]):
                continue
            methods.append((i, mm.start(2)))
        found.append((path, text, kind, (line, m.start(3)), methods))
    return found


def uri(path):
    return path.as_uri()


def pos_params(path, p):
    return {'textDocument': {'uri': uri(path)}, 'position': {'line': p[0], 'character': p[1]}}


def fast(path, p):
    r = timed('workspace/executeCommand', {'command': 'openidea.java.implementations', 'arguments': [uri(path), p[0], p[1]]})
    if r['ok']:
        res = r.pop('result') or {}
        r['n'], r['complete'] = len(res.get('locations') or []), bool(res.get('complete'))
    return r


def standard(path, p):
    r = timed('textDocument/implementation', pos_params(path, p))
    if r['ok']:
        r['n'] = len(r.pop('result') or [])
    return r


def app_goto(path, p):
    """ide-lsp's order (`goto_in`): the sources first; the standard request unless complete and non-empty."""
    t0 = time.monotonic()
    f = fast(path, p)
    s = None if f.get('complete') and f.get('n') else standard(path, p)
    return {'ms': round((time.monotonic() - t0) * 1000), 'fast': f, 'standard': s}


def hierarchy(path, p, direction):
    t0 = time.monotonic()
    prep = timed('textDocument/prepareTypeHierarchy', pos_params(path, p))
    if not prep['ok'] or not prep.get('result'):
        return {'ok': False, 'ms': prep['ms'], 'error': prep.get('error', 'no item')}
    r = timed(f'typeHierarchy/{direction}', {'item': prep['result'][0]})
    r['prepare_ms'] = prep['ms']
    r['n'] = len(r.pop('result', None) or [])
    r['ms'] = round((time.monotonic() - t0) * 1000)
    return r


def summary(rows, key='ms'):
    v = [r[key] for r in rows if r and r.get(key) is not None]
    return {'n': len(v), 'median': statistics.median(v), 'max': max(v)} if v else {'n': 0}


def under_load(name, jobs, manual):
    """`jobs` four at a time (ide-lsp's IMPLEMENTATION_CONCURRENCY); one Go to Implementation meanwhile."""
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        t0 = time.monotonic()
        futures = [pool.submit(fn, *args) for fn, args in jobs]
        time.sleep(0.3)
        m = app_goto(*manual)
        done = [f.result() for f in futures]
    return {'load': name, 'load_ms': round((time.monotonic() - t0) * 1000), 'manual_ms': m['ms'], 'manual': m, 'jobs': summary(done)}


report = {'os': f'{system} {machine}', 'cpus': os.cpu_count(), 'project': project.name}
try:
    init = timed('initialize', {
        'processId': None, 'rootUri': uri(project), 'workspaceFolders': [{'uri': uri(project), 'name': project.name}],
        'capabilities': {'textDocument': {'typeHierarchy': {}, 'implementation': {}}, 'workspace': {'configuration': True}},
        'initializationOptions': {'bundles': [str(bundle)], 'extendedClientCapabilities': {'classFileContentsSupport': True},
                                  'settings': {'java': {'import': {'gradle': {'enabled': False}}}}}}, timeout=180)
    assert init['ok'], init
    assert 'openidea.java.implementations' in init['result']['capabilities']['executeCommandProvider']['commands']
    send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})
    report['service_ready'] = wait_status('ServiceReady', 1800)
    report['import_s'] = round(time.monotonic() - t_start)
    build = timed('java/buildWorkspace', False, timeout=1800)
    report['build_ms'], report['build'] = build['ms'], build.get('result', build.get('error'))

    projects = timed('workspace/executeCommand', {'command': 'java.project.getAll', 'arguments': []}).get('result') or []
    jars = set()
    for p in projects:
        cp = timed('workspace/executeCommand', {'command': 'java.project.getClasspaths', 'arguments': [p, json.dumps({'scope': 'test'})]})
        jars.update(e for e in ((cp.get('result') or {}).get('classpaths') or []) if e.endswith('.jar'))
    report['projects'], report['jars'] = len(projects), len(jars)

    files = scan()
    report['source_files'] = len(files)
    rng = random.Random(2693)

    def pick(kind, n):
        pool = [(f[0], m) for f in files if f[2] == kind for m in f[4][:3]]
        rng.shuffle(pool)
        return pool[:n]

    concrete, iface = pick('class', 24), pick('interface', 12)
    types = [(f[0], f[3]) for f in files if f[2] in ('class', 'interface')]
    rng.shuffle(types)
    types = types[:12]
    texts = {f[0]: f[1] for f in files}
    for p in {p for p, _ in concrete + iface + types}:
        send({'jsonrpc': '2.0', 'method': 'textDocument/didOpen',
              'params': {'textDocument': {'uri': uri(p), 'languageId': 'java', 'version': 1, 'text': texts[p]}}})
    time.sleep(5)

    def at(p, pos):
        return f'{p.relative_to(project).as_posix()}:{pos[0] + 1}'

    iso = report['isolated'] = {}
    iso['concrete'] = [{'at': at(p, pos), **app_goto(p, pos)} for p, pos in concrete[:12]]
    iso['interface'] = [{'at': at(p, pos), **app_goto(p, pos)} for p, pos in iface[:6]]
    iso['standard_again'] = [{'at': at(p, pos), **standard(p, pos)} for p, pos in concrete[:3]]
    iso['subtypes'] = [{'at': at(p, pos), **hierarchy(p, pos, 'subtypes')} for p, pos in types[:6]]
    iso['supertypes'] = [{'at': at(p, pos), **hierarchy(p, pos, 'supertypes')} for p, pos in types[:6]]

    manual = iface[0] if iface else concrete[0]
    report['manual_at'] = at(*manual)
    report['manual_alone'] = [app_goto(*manual) for _ in range(3)]
    report['under_load'] = [
        under_load('12 x textDocument/implementation', [(standard, x) for x in concrete[12:24]], manual),
        under_load('6 x typeHierarchy/subtypes', [(hierarchy, (p, pos, 'subtypes')) for p, pos in types[6:12]], manual),
    ]
    rows = iso['concrete']
    report['summary'] = {
        'concrete: fast command ms': summary([r['fast'] for r in rows]),
        'concrete: standard request ms': summary([r['standard'] for r in rows]),
        'concrete: fell back to standard': f"{sum(1 for r in rows if r['standard'])}/{len(rows)}",
        'concrete: fallback found anything': sum(1 for r in rows if (r['standard'] or {}).get('n')),
        'standard request again ms': summary(iso['standard_again']),
        'interface: Go to Implementation ms': summary(iso['interface']),
        'subtypes ms': summary(iso['subtypes']),
        'supertypes ms': summary(iso['supertypes']),
        'manual alone ms': summary(report['manual_alone']),
        'manual under load ms': {l['load']: l['manual_ms'] for l in report['under_load']},
    }
except Exception as e:
    report['error'] = repr(e)
    raise
finally:
    report['statuses'] = [(round(t, 1), p.get('type'), p.get('message')) for t, p in statuses][-40:]
    out.write_text(json.dumps(report, indent=1, default=str))
    keys = ['os', 'cpus', 'project', 'projects', 'jars', 'source_files', 'import_s', 'build_ms', 'error']
    md = [f'### {report["project"]} on {report["os"]}', '', '| | |', '|---|---|']
    md += [f'| {k} | {report[k]} |' for k in keys if k in report]
    md += [f'| {k} | {json.dumps(v)} |' for k, v in report.get('summary', {}).items()]
    print('\n'.join(md))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write('\n'.join(md) + '\n\n')
    jdt_log = work / 'metadata' / '.metadata' / '.log'
    if jdt_log.is_file():
        shutil.copy(jdt_log, out.with_suffix('.jdtls.log'))
    proc.kill()
    log.close()
    shutil.rmtree(work, ignore_errors=True)
