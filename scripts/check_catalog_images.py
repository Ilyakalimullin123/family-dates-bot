"""Verify the bundled Kaspi image sources without sending Telegram messages."""
import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor


def check(item):
    url = item['image_url']
    assert urlsplit(url).hostname == 'resources.cdn-kaspi.kz'
    with urlopen(Request(url, headers={'User-Agent': 'FamilyDatesCatalog/1.0'}), timeout=20) as response:
        data = response.read(5_000_001)
        mime = response.headers.get_content_type()
        assert mime in ('image/jpeg', 'image/png', 'image/webp'), mime
        assert 100 < len(data) < 5_000_000
        assert data[:3] == b'\xff\xd8\xff' or data[:8] == b'\x89PNG\r\n\x1a\n' or data[:4] == b'RIFF'
    return item['title'], len(data), mime


if __name__ == '__main__':
    items = json.loads((Path(__file__).resolve().parents[1] / 'catalog/products.json').read_text())
    with ThreadPoolExecutor(max_workers=3) as pool:
        for title, size, mime in pool.map(check, items):
            print(f'OK {size} bytes {mime} {title}')
    print(f'PASS: {len(items)} real product images available')
