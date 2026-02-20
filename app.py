import os
import re
import uuid
import zipfile
import tempfile
from pathlib import Path
from flask import Flask, request, jsonify, send_file, abort
from playwright.sync_api import sync_playwright

app = Flask(__name__, static_folder='public', static_url_path='')

TEMP_DIR = Path(__file__).parent / 'temp'
TEMP_DIR.mkdir(exist_ok=True)

ID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


@app.route('/')
def index():
    return app.send_static_file('index.html')


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

            # https:// がない場合は付加
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
