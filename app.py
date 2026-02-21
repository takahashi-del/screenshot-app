import os
import re
import uuid
import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse
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
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'}

# ヘッドレス Chrome がボットと検知されないための起動オプション
BROWSER_ARGS = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-blink-features=AutomationControlled',  # webdriver 検知回避
    '--hide-scrollbars',
    '--force-color-profile=srgb',                    # 色再現を sRGB に統一
    '--disable-font-subpixel-positioning',            # フォント位置を整数化して安定
    '--lang=ja-JP',
]


# ---- ファイル名生成 ----

def make_download_name(url, title):
    """URL + ページタイトルから安全なファイル名を生成する"""
    parsed = urlparse(url)
    domain = re.sub(r'^www\.', '', parsed.netloc).replace('.', '-')
    path = parsed.path.strip('/').replace('/', '-')

    # タイトルをファイル名として安全な文字列に変換
    safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]', '', title).strip()
    safe_title = re.sub(r'[\s\u3000]+', '_', safe_title)[:60]

    if safe_title:
        name = f'{domain}_{safe_title}'
    elif path:
        name = f'{domain}_{path[:40]}'
    else:
        name = f'{domain}_top'

    name = re.sub(r'[-_]{2,}', '_', name)
    return f'{name}.png'


# ---- サイトマップ関連ヘルパー ----

def fetch_sitemap_urls(base_url):
    """sitemap.xml からURLの一覧を取得して返す（最大100件）"""
    candidates = []

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
        browser = p.chromium.launch(
            headless=True,
            args=BROWSER_ARGS,
        )

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

                # 一般的なブラウザになりすます（ボット検知回避）
                page.set_extra_http_headers({
                    'Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                })

                # navigator.webdriver を undefined にして検知回避
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                """)

                # DOM 読み込み完了まで待機
                page.goto(target_url, wait_until='domcontentloaded', timeout=30000)

                # ネットワークが落ち着くまで待つ（タイムアウトしても続行）
                try:
                    page.wait_for_load_state('networkidle', timeout=8000)
                except Exception:
                    pass

                # Web フォント読み込み完了を待つ
                try:
                    page.evaluate('() => document.fonts.ready')
                except Exception:
                    pass

                # アニメーション・トランジションを停止して見た目を固定
                page.add_style_tag(content="""
                    *, *::before, *::after {
                        animation-duration: 0.001s !important;
                        animation-delay: 0s !important;
                        transition-duration: 0.001s !important;
                    }
                """)

                # ページ全体をゆっくりスクロールして遅延読み込み画像を発火させる
                page.evaluate("""
                    async () => {
                        const step = 300;
                        const delay = 120;
                        const total = document.body.scrollHeight;
                        let pos = 0;
                        while (pos < total) {
                            window.scrollTo(0, pos);
                            await new Promise(r => setTimeout(r, delay));
                            pos += step;
                        }
                        window.scrollTo(0, 0);
                        await new Promise(r => setTimeout(r, 400));
                    }
                """)

                # すべての <img> の読み込み完了を待つ
                try:
                    page.wait_for_function(
                        "() => Array.from(document.images).every(img => img.complete)",
                        timeout=6000,
                    )
                except Exception:
                    pass

                # 最終的な描画が落ち着くのを待つ
                page.wait_for_timeout(1000)

                # ページタイトルを取得してファイル名に使う
                page_title = page.title()
                download_name = make_download_name(url, page_title)

                page.screenshot(path=str(file_path), full_page=True)
                page.close()

                # メタ情報を保存（ダウンロード時のファイル名に使用）
                meta_path = TEMP_DIR / f'{file_id}.meta'
                meta_path.write_text(
                    json.dumps({'download_name': download_name, 'title': page_title}, ensure_ascii=False),
                    encoding='utf-8',
                )

                results.append({'url': url, 'id': file_id, 'status': 'ok', 'title': page_title, 'download_name': download_name})
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

    # メタファイルからダウンロードファイル名を取得
    download_name = f'screenshot-{file_id}.png'
    meta_path = TEMP_DIR / f'{file_id}.meta'
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            download_name = meta.get('download_name', download_name)
        except Exception:
            pass

    return send_file(
        str(file_path),
        mimetype='image/png',
        as_attachment=True,
        download_name=download_name,
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
            if not file_path.exists():
                continue

            # ZIP 内のファイル名もタイトルベースにする
            arc_name = f'screenshot-{file_id}.png'
            meta_path = TEMP_DIR / f'{file_id}.meta'
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding='utf-8'))
                    arc_name = meta.get('download_name', arc_name)
                except Exception:
                    pass

            zf.write(str(file_path), arc_name)

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
