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


def normalize_url(u):
    u = u.strip()
    if not u:
        return u
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', u):
        u = 'https://' + u
    return u


def extract_image_urls(page_url, html):
    """Aggressively pull image URLs from every possible source."""
    soup = BeautifulSoup(html, 'html.parser')
    found = []

    def add(u):
        if not u:
            return
        u = u.strip().strip('"\'')
        if not u or u.startswith('data:'):
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

    # <a href> and any tag/attribute whose value points at an image
    for tag in soup.find_all(True):
        for attr_val in tag.attrs.values():
            vals = attr_val if isinstance(attr_val, list) else [attr_val]
            for v in vals:
                if isinstance(v, str) and IMG_EXT_RE.search(v):
                    add(v)

    # inline style + <style> background-image: url(...)
    for _, u in re.findall(r'url\((["\']?)(.*?)\1\)', html, re.I):
        if u and not u.startswith('data:'):
            add(u)

    # meta og:image / twitter:image
    for meta in soup.find_all('meta'):
        prop = (meta.get('property') or meta.get('name') or '').lower()
        if prop in ('og:image', 'twitter:image') and meta.get('content'):
            add(meta['content'])

    # <noscript> often holds real <img> for lazy sites
    for ns in soup.find_all('noscript'):
        for m in re.findall(r'src=["\']([^"\']+)["\']', ns.decode_contents(), re.I):
            add(m)

    # Raw regex sweep of the entire HTML/JSON (catches JS-embedded URLs)
    for m in re.findall(r'https?:\\?/\\?/[^\s"\'<>()]+?\.(?:jpe?g|png|gif|webp|svg|bmp|avif|tiff?)(?:\?[^\s"\'<>()]*)?', html, re.I):
        add(m.replace('\\/', '/'))

    # dedupe, preserve order
    seen, uniq = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def extract_links(page_url, html, same_domain_only=True):
    """Return same-domain page links for crawling."""
    soup = BeautifulSoup(html, 'html.parser')
    base_host = urlparse(page_url).netloc
    links = []
    seen = set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith(('mailto:', 'tel:', 'javascript:', '#')):
            continue
        absu = urljoin(page_url, href)
        if not is_valid_url(absu):
            continue
        if same_domain_only and urlparse(absu).netloc != base_host:
            continue
        absu = absu.split('#')[0]
        if absu not in seen:
            seen.add(absu)
            links.append(absu)
    return links


def crawl_for_images(start_url, max_images, depth, max_pages):
    """BFS crawl same-domain pages, collecting image URLs."""
    visited = set()
    queue = [(start_url, 0)]
    all_imgs, img_seen = [], set()
    pages_done = 0

    while queue and pages_done < max_pages and len(all_imgs) < max_images:
        url, d = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  Error fetching {url}: {e}")
            continue
        pages_done += 1
        html = resp.text
        imgs = extract_image_urls(url, html)
        new = 0
        for u in imgs:
            if u not in img_seen:
                img_seen.add(u)
                all_imgs.append(u)
                new += 1
        print(f"  [page {pages_done}] {url[:70]} -> +{new} images (total {len(all_imgs)})")
        if d < depth:
            for link in extract_links(url, html):
                if link not in visited:
                    queue.append((link, d + 1))

    return all_imgs[:max_images]


def search_bing_images(query, max_images):
    """Scrape Bing Images search results (full-res murl URLs). SafeSearch OFF."""
    from urllib.parse import quote_plus
    found, seen = [], set()

    # Set cookies to force SafeSearch OFF
    session.cookies.set('_SS', 'SRCHHPGUSR=ADLT=OFF&SRCHHPGUSR=ADLT=OFF', domain='.bing.com')
    session.cookies.set('SRCHHPGUSR', 'ADLT=OFF', domain='.bing.com')
    session.cookies.set('_EDGE_S', 'mkt=en-us&ui=en-us&ADLT=off', domain='.bing.com')

    # Try multiple query variations to get past early stop
    query_variants = [query, f"{query} photos", f"{query} images", f"{query} HD",
                      f"{query} wallpaper", f"{query} picture", f"{query} pic",
                      f"{query} photo gallery", f'"{query}"', f"{query} site:pinterest.com"]
    variant_idx = 0
    first = 1
    page = 0
    consecutive_empty = 0

    while len(found) < max_images and variant_idx < len(query_variants):
        page += 1
        q = query_variants[variant_idx]
        search_url = (
            f"https://www.bing.com/images/search?q={quote_plus(q)}"
            f"&first={first}&count=35&form=HDRSC2&safesearch=off&adlt=off"
        )
        try:
            resp = session.get(search_url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  Bing search error: {e}")
            variant_idx += 1
            first = 1
            page = 0
            continue
        html = resp.text
        murls = re.findall(r'&quot;murl&quot;:&quot;(.*?)&quot;', html)
        murls += re.findall(r'"murl":"(.*?)"', html)
        new = 0
        for u in murls:
            u = u.replace('\\/', '/').replace('&amp;', '&')
            if u and u not in seen and is_valid_url(u):
                seen.add(u)
                found.append(u)
                new += 1
                if len(found) >= max_images:
                    break
        print(f"  [Bing q='{q}' p{page}] +{new} images (total {len(found)})")
        if new == 0:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                # Move to next query variant
                variant_idx += 1
                first = 1
                page = 0
                consecutive_empty = 0
                print(f"  Switching to query variant: {query_variants[variant_idx] if variant_idx < len(query_variants) else 'done'}")
            continue
        consecutive_empty = 0
        first += 35
    return found[:max_images]


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
    search_query = os.getenv('SEARCH_QUERY', '').strip()
    max_images = int(os.getenv('MAX_IMAGES', '500'))
    workers = int(os.getenv('WORKERS', '24'))
    depth = int(os.getenv('CRAWL_DEPTH', '1'))
    max_pages = int(os.getenv('MAX_PAGES', '40'))

    output_dir = 'downloaded_images'
    os.makedirs(output_dir, exist_ok=True)

    all_imgs, seen = [], set()

    if search_query:
        # --- SEARCH MODE: scrape Bing Images ---
        print(f"Search mode | query: '{search_query}' | max {max_images} | {workers} workers")
        for u in search_bing_images(search_query, max_images):
            if u not in seen:
                seen.add(u)
                all_imgs.append(u)
    else:
        # --- URL/CRAWL MODE ---
        if not urls_str:
            print("Error: No URLs or search query provided")
            sys.exit(1)
        urls = [normalize_url(u) for u in urls_str.split(',') if u.strip()]
        if not urls:
            print("Error: No valid URLs provided")
            sys.exit(1)
        print(f"Scraping {len(urls)} URL(s) | max {max_images} | depth {depth} | "
              f"max {max_pages} pages | {workers} workers")
        for i, url in enumerate(urls, 1):
            print(f"\n[{i}/{len(urls)}] Crawling: {url}")
            remaining = max_images - len(all_imgs)
            if remaining <= 0:
                break
            for u in crawl_for_images(url, remaining, depth, max_pages):
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
