import os
import re
import uuid
import zipfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_file, abort
from playwright.sync_api import sync_playwright

app = Flask(__name__, static_folder='public', static_url_path='')

TEMP_DIR = Path(__file__).parent / 'temp'
TEMP_DIR.mkdir(exist_ok=True)

ID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
SITEMAP_NS = 'http://www.sitemaps.org/schemas/sitemap/0.9'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}


# ---- サイトマップ関連ヘルパー ----

def fetch_sitemap_urls(base_url):
    """sitemap.xml からURLの一覧を取得して返す（最大100件）"""
    candidates = []

    # robots.txt から Sitemap: 行を探す
    try:
        r = requests.get(f'{base_url}/robots.txt', timeout=5, headers=UA)
        if r.status_code == 200:
            for line in r.text.splitlines():
                if line.lower().startswith('sitemap:'):
                    candidates.append(line.split(':', 1)[1].strip())
    except Exception:
        pass

    candidates += [f'{base_url}/sitemap.xml', f'{base_url}/sitemap_index.xml']

    urls = []
    for sitemap_url in candidates:
        try:
            r = requests.get(sitemap_url, timeout=10, headers=UA)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            local_tag = root.tag.split('}')[-1] if '}' in root.tag else root.tag

            if local_tag == 'sitemapindex':
                sub_locs = [e.text.strip() for e in root.findall(f'.//{{{SITEMAP_NS}}}sitemap/{{{SITEMAP_NS}}}loc')]
                for sub_url in sub_locs[:5]:
                    try:
                        sub_r = requests.get(sub_url, timeout=10, headers=UA)
                        if sub_r.status_code == 200:
                            sub_root = ET.fromstring(sub_r.content)
                            urls += [e.text.strip() for e in sub_root.findall(f'.//{{{SITEMAP_NS}}}url/{{{SITEMAP_NS}}}loc')]
                    except Exception:
                        continue
                    if len(urls) >= 100:
                        break
            elif local_tag == 'urlset':
                urls = [e.text.strip() for e in root.findall(f'.//{{{SITEMAP_NS}}}url/{{{SITEMAP_NS}}}loc')]

            if urls:
                break
        except Exception:
            continue

    return urls[:100]


def fetch_page_title(url):
    """ページの <title> タグを取得する（軽量・ストリーミング）"""
    try:
        with requests.get(url, timeout=6, headers=UA, stream=True, allow_redirects=True) as r:
            if r.status_code != 200:
                return ''
            content = b''
            for chunk in r.iter_content(chunk_size=4096):
                content += chunk
                lower = content.lower()
                if b'</title>' in lower or b'</head>' in lower:
                    break
                if len(content) > 32768:
                    break
        soup = BeautifulSoup(content, 'html.parser')
        tag = soup.find('title')
        return tag.get_text(strip=True) if tag else ''
    except Exception:
        return ''


# ---- ルート ----

@app.route('/')
def index():
    return app.send_static_file('index.html')


@app.route('/sitemap', methods=['POST'])
def get_sitemap():
    data = request.get_json()
    raw_url = (data.get('url') or '').strip()

    if not raw_url:
        return jsonify({'error': 'URLを入力してください'}), 400

    if not re.match(r'^https?://', raw_url, re.IGNORECASE):
        raw_url = 'https://' + raw_url

    base_url = raw_url.rstrip('/')
    urls = fetch_sitemap_urls(base_url)

    if not urls:
        return jsonify({'error': 'サイトマップが見つかりませんでした（sitemap.xml が存在しないか、アクセスできません）'}), 404

    # タイトルを並列取得
    title_map = {}
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(fetch_page_title, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                title_map[url] = future.result()
            except Exception:
                title_map[url] = ''

    pages = [{'url': url, 'title': title_map.get(url) or url} for url in urls]
    return jsonify({'pages': pages})


@app.route('/screenshot', methods=['POST'])
def screenshot():
    data = request.get_json()
    urls = data.get('urls', [])

    if not isinstance(urls, list) or len(urls) == 0:
        return jsonify({'error': 'URLを1つ以上指定してください'}), 400

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch()

        for raw_url in urls:
            url = raw_url.strip()
            if not url:
                continue

            file_id = str(uuid.uuid4())
            file_path = TEMP_DIR / f'{file_id}.png'

            if not re.match(r'^https?://', url, re.IGNORECASE):
                target_url = f'https://{url}'
            else:
                target_url = url

            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 900})
                page.goto(target_url, wait_until='networkidle', timeout=30000)
                page.screenshot(path=str(file_path), full_page=True)
                page.close()
                results.append({'url': url, 'id': file_id, 'status': 'ok'})
            except Exception as e:
                results.append({'url': url, 'id': None, 'status': 'error', 'error': str(e)})

        browser.close()

    return jsonify({'results': results})


@app.route('/download/<file_id>')
def download(file_id):
    if not ID_PATTERN.match(file_id):
        abort(400)

    file_path = TEMP_DIR / f'{file_id}.png'
    if not file_path.exists():
        abort(404)

    return send_file(
        str(file_path),
        mimetype='image/png',
        as_attachment=True,
        download_name=f'screenshot-{file_id}.png'
    )


@app.route('/download-zip', methods=['POST'])
def download_zip():
    data = request.get_json()
    ids = data.get('ids', [])

    if not ids:
        abort(400)

    zip_path = TEMP_DIR / f'{uuid.uuid4()}.zip'

    with zipfile.ZipFile(str(zip_path), 'w', zipfile.ZIP_DEFLATED) as zf:
        for file_id in ids:
            if not ID_PATTERN.match(str(file_id)):
                continue
            file_path = TEMP_DIR / f'{file_id}.png'
            if file_path.exists():
                zf.write(str(file_path), f'screenshot-{file_id}.png')

    return send_file(
        str(zip_path),
        mimetype='application/zip',
        as_attachment=True,
        download_name='screenshots.zip'
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    print(f'サーバー起動: http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
