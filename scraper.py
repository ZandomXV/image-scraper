#!/usr/bin/env python3
import os
import re
import sys
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
HEADERS = {'User-Agent': UA, 'Accept': 'text/html,image/*,*/*'}
IMG_EXT_RE = re.compile(r'\.(jpe?g|png|gif|webp|svg|bmp|tiff?|avif|ico)(\?.*)?$', re.I)
LAZY_ATTRS = ['src', 'data-src', 'data-lazy-src', 'data-original', 'data-lazy',
              'data-srcset', 'data-original-src', 'data-hi-res-src', 'data-full-src']

session = requests.Session()
session.headers.update(HEADERS)


def is_valid_url(url):
    try:
        r = urlparse(url)
        return r.scheme in ('http', 'https') and bool(r.netloc)
    except Exception:
        return False


def pick_from_srcset(srcset):
    """Return the largest candidate from a srcset string."""
    best, best_w = None, -1
    for part in srcset.split(','):
        seg = part.strip().split()
        if not seg:
            continue
        u = seg[0]
        w = 0
        if len(seg) > 1 and seg[1].endswith('w'):
            try:
                w = int(seg[1][:-1])
            except ValueError:
                w = 0
        if w >= best_w:
            best_w, best = w, u
    return best


def extract_image_urls(page_url, html):
    """Aggressively pull image URLs from many sources."""
    soup = BeautifulSoup(html, 'html.parser')
    found = []

    def add(u):
        if not u:
            return
        u = u.strip()
        if u.startswith('data:'):
            return
        absu = urljoin(page_url, u)
        if is_valid_url(absu):
            found.append(absu)

    # <img> and lazy-load attributes
    for img in soup.find_all('img'):
        for attr in LAZY_ATTRS:
            val = img.get(attr)
            if not val:
                continue
            if 'srcset' in attr:
                add(pick_from_srcset(val))
            else:
                add(val)
        if img.get('srcset'):
            add(pick_from_srcset(img.get('srcset')))

    # <picture><source srcset>
    for source in soup.find_all('source'):
        if source.get('srcset'):
            add(pick_from_srcset(source.get('srcset')))
        if source.get('data-srcset'):
            add(pick_from_srcset(source.get('data-srcset')))

    # <a href> pointing directly at images
    for a in soup.find_all('a', href=True):
        if IMG_EXT_RE.search(a['href']):
            add(a['href'])

    # inline style + <style> background-image
    style_urls = re.findall(r'url\((["\']?)(.*?)\1\)', html, re.I)
    for _, u in style_urls:
        if u and not u.startswith('data:'):
            add(u)

    # meta og:image / twitter:image
    for meta in soup.find_all('meta'):
        prop = (meta.get('property') or meta.get('name') or '').lower()
        if prop in ('og:image', 'twitter:image') and meta.get('content'):
            add(meta['content'])

    # dedupe, preserve order
    seen, uniq = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def get_image_urls(url, max_images):
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        urls = extract_image_urls(url, resp.text)
        return urls[:max_images]
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return []


def ext_for(url, content_type):
    ct = (content_type or '').lower()
    for key, ext in [('jpeg', '.jpg'), ('jpg', '.jpg'), ('png', '.png'),
                     ('gif', '.gif'), ('webp', '.webp'), ('svg', '.svg'),
                     ('avif', '.avif'), ('bmp', '.bmp'), ('icon', '.ico')]:
        if key in ct:
            return ext
    m = IMG_EXT_RE.search(url)
    if m:
        return '.' + m.group(1).lower().replace('jpeg', 'jpg')
    return '.jpg'


def download_image(url, output_dir, seen_hashes):
    try:
        resp = session.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        content = resp.content
        if not content:
            return False
        h = hashlib.md5(content).hexdigest()
        if h in seen_hashes:
            return False
        seen_hashes.add(h)
        ext = ext_for(url, resp.headers.get('content-type', ''))
        filename = f"image_{h[:12]}{ext}"
        with open(os.path.join(output_dir, filename), 'wb') as f:
            f.write(content)
        print(f"Downloaded: {filename}  <- {url[:80]}")
        return True
    except Exception as e:
        print(f"Error downloading {url}: {e}")
        return False


def main():
    urls_str = os.getenv('URLS', '')
    max_images = int(os.getenv('MAX_IMAGES', '500'))
    workers = int(os.getenv('WORKERS', '16'))
    if not urls_str:
        print("Error: No URLs provided")
        sys.exit(1)
    urls = [u.strip() for u in urls_str.split(',') if u.strip()]
    if not urls:
        print("Error: No valid URLs provided")
        sys.exit(1)

    output_dir = 'downloaded_images'
    os.makedirs(output_dir, exist_ok=True)
    print(f"Scraping {len(urls)} URL(s) | max {max_images}/URL | {workers} workers")

    # Collect all image URLs first, dedupe across pages
    all_imgs, seen = [], set()
    for i, url in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] Scanning: {url}")
        imgs = get_image_urls(url, max_images)
        print(f"  Found {len(imgs)} image link(s)")
        for u in imgs:
            if u not in seen:
                seen.add(u)
                all_imgs.append(u)

    print(f"\nTotal unique image links: {len(all_imgs)}")
    print("Downloading concurrently...")

    seen_hashes = set()
    downloaded = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(download_image, u, output_dir, seen_hashes): u for u in all_imgs}
        for fut in as_completed(futures):
            if fut.result():
                downloaded += 1

    print(f"\nTotal images downloaded: {downloaded}")
    print(f"Images saved to: {output_dir}/")


if __name__ == '__main__':
    main()
