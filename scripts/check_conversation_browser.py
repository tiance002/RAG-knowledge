"""Real Chrome acceptance of backend history with an isolated synthetic API."""
import argparse
import hashlib
import json
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

KEY = 'rag-workspace.chat-history.v1'
SOURCE = 'Atlas synthetic launch policy: launch budget is 420 units. Atlas review owner is Mira. Synthetic evidence only.'


def run(base, out):
    out.mkdir(parents=True, exist_ok=True)
    original = json.dumps([
        {'id': 'browser-old-atlas', 'title': 'Legacy Atlas', 'updatedAt': 1,
         'model': 'acceptance-model', 'kbIds': ['acceptance-atlas'], 'mode': 'normal',
         'messages': [{'role': 'user', 'content': 'Who reviews Atlas?'},
                      {'role': 'assistant', 'content': 'Mira [1].', 'citations': [
                          {'chunkId': 'atlas-chunk', 'content': SOURCE, 'fileName': 'synthetic-atlas.txt'}]}]},
        {'id': 'browser-malformed', 'title': 'Malformed archive', 'messages': [None]},
    ], ensure_ascii=False, indent=2)
    checks, errors, posted, network = [], [], [], []

    def check(name, condition):
        checks.append({'name': name, 'pass': bool(condition)})
        if not condition:
            raise AssertionError(name)

    with sync_playwright() as p, httpx.Client(base_url=base, trust_env=False, timeout=30) as client:
        browser = p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe', headless=True)
        context = browser.new_context(accept_downloads=True)
        context.add_init_script('if (localStorage.getItem(' + json.dumps(KEY) + ') === null) localStorage.setItem(' + json.dumps(KEY) + ',' + json.dumps(original) + ');')
        page = context.new_page()
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.on('response', lambda response: network.append({'url': response.url, 'status': response.status}))
        page.on('request', lambda req: posted.append({'url': req.url, 'body': req.post_data_json})
                if req.method == 'POST' and req.url.endswith('/turns') else None)
        try:
            page.goto(base + '/chat'); page.wait_for_load_state('networkidle')
            page.screenshot(path=str(out / 'before-import.png'), full_page=True)
            check('legacy_visible', page.get_by_title('Legacy Atlas', exact=True).count() == 1)
            with page.expect_download() as download:
                page.get_by_role('button', name='导出原始备份', exact=True).first.click()
            downloaded = download.value
            downloaded.save_as(str(out / 'browser-original.json'))
            check('export_exact_original', (out / 'browser-original.json').read_bytes() == original.encode('utf-8'))
            page.get_by_role('button', name='导入服务端', exact=True).click()
            page.get_by_role('status').filter(has_text='导入结果').wait_for(timeout=20000)
            sessions = client.get('/api/v1/chat/sessions').json()['items']
            imported = next(s for s in sessions if s.get('legacy_origin', {}).get('original_id') == 'browser-old-atlas')
            check('partial_import_valid_saved', len([s for s in sessions if s.get('legacy_origin')]) == 1)
            before = len(sessions)
            page.get_by_role('button', name='导入服务端', exact=True).click()
            page.get_by_role('status').filter(has_text='导入结果').wait_for(timeout=20000)
            page.wait_for_load_state('networkidle')
            check('import_idempotent', len(client.get('/api/v1/chat/sessions').json()['items']) == before)
            config = {**imported['config_overrides'], 'search_mode': 'fulltext'}
            patched = client.patch('/api/v1/chat/sessions/' + imported['id'], json={'expected_revision': imported['revision'], 'config_overrides': config})
            patched.raise_for_status()
            page.goto(base + '/chat/' + imported['id']); page.wait_for_load_state('networkidle')
            page.get_by_role('combobox').filter(has=page.locator('option[value="acceptance-model"]')).select_option('acceptance-model')
            page.get_by_label('输入问题或描述任务', exact=True).fill('What is the Atlas launch budget?')
            with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/turns')):
                page.get_by_role('button', name='发送', exact=True).click()
            page.get_by_role('button', name='停止生成', exact=True).wait_for(state='hidden', timeout=30000)
            page.wait_for_load_state('networkidle')
            turns = client.get('/api/v1/chat/sessions/' + imported['id'] + '/turns').json()['items']
            check('browser_question_persisted', turns[-1]['user_content'] == 'What is the Atlas launch budget?' and turns[-1]['generation_status'] == 'COMPLETED')
            check('question_only_payload', bool(posted) and all(set(x['body']) == {'question', 'client_request_id', 'expected_revision'} for x in posted))
            check('answer_and_refs_saved', '420' in turns[-1]['assistant_content'] and bool(turns[-1]['citations']))
            page.reload(); page.wait_for_load_state('networkidle')
            page.get_by_text('What is the Atlas launch budget?', exact=True).wait_for()
            check('refresh_preserves', '420' in page.locator('.chat-thread').inner_text())
            fresh = browser.new_context(); second = fresh.new_page()
            second.goto(base + '/chat/' + imported['id']); second.wait_for_load_state('networkidle')
            second.get_by_text('What is the Atlas launch budget?', exact=True).wait_for()
            check('new_browser_context_reads_server', second.evaluate('localStorage.getItem(' + json.dumps(KEY) + ')') is None)
            fresh.close()
            check('original_localstorage_unchanged', page.evaluate('localStorage.getItem(' + json.dumps(KEY) + ')') == original)
            check('no_browser_runtime_errors', not errors)
            page.screenshot(path=str(out / 'after-refresh.png'), full_page=True)
        finally:
            page.screenshot(path=str(out / 'last-page.png'), full_page=True)
            (out / 'last-page.html').write_text(page.content(), encoding='utf-8')
            (out / 'browser-results.json').write_text(json.dumps({'checks': checks, 'page_errors': errors,
                'turn_requests': posted, 'network': network, 'original_sha256': hashlib.sha256(original.encode()).hexdigest()}, ensure_ascii=False, indent=2), encoding='utf-8')
            browser.close()
    print(json.dumps({'checks': len(checks), 'passed': sum(x['pass'] for x in checks), 'failed': sum(not x['pass'] for x in checks)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--base-url', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(); run(args.base_url, args.out)
