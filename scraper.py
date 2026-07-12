#!/usr/bin/env python3
import os
import re
import sys
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, quote_plus, urlencode
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

    for source in soup.find_all('source'):
        if source.get('srcset'):
            add(pick_from_srcset(source.get('srcset')))
        if source.get('data-srcset'):
            add(pick_from_srcset(source.get('data-srcset')))

    for tag in soup.find_all(True):
        for av in tag.attrs.values():
            vals = av if isinstance(av, list) else [av]
            for v in vals:
                if isinstance(v, str) and IMG_EXT_RE.search(v):
                    add(v)

    for _, u in re.findall(r'url\((["\']?)(.*?)\1\)', html, re.I):
        if u and not u.startswith('data:'):
            add(u)

    for meta in soup.find_all('meta'):
        prop = (meta.get('property') or meta.get('name') or '').lower()
        if prop in ('og:image', 'twitter:image') and meta.get('content'):
            add(meta['content'])

    for ns in soup.find_all('noscript'):
        for m in re.findall(r'src=["\']([^"\']+)["\']', ns.decode_contents(), re.I):
            add(m)

    for m in re.findall(r'https?:\\?/\\?/[^\s"\'<>()]+?\.(?:jpe?g|png|gif|webp|svg|bmp|avif|tiff?)(?:\?[^\s"\'<>()]*)?', html, re.I):
        add(m.replace('\\/', '/'))

    seen, uniq = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def extract_links(page_url, html, same_domain_only=True):
    soup = BeautifulSoup(html, 'html.parser')
    base_host = urlparse(page_url).netloc
    links, seen = [], set()
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
    visited = set()
    queue = [(start_url, 0)]
    all_imgs, img_seen = [], set()
    pages_done = 0
    while queue and len(all_imgs) < max_images and pages_done < max_pages:
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


def search_images(query, max_images):
    """Multi-engine image search with SafeSearch OFF."""
    found, seen = [], set()

    query_variants = [query, f"{query} photos", f"{query} images", f"{query} HD",
                      f"{query} wallpaper", f"{query} picture", f"{query} pic",
                      f"{query} high quality", f'"{query}"', f"{query} site:imgur.com"]

    # --- Engine 0: SearXNG meta-search (aggregates Google/Bing/Yahoo, safesearch=0) ---
    searx_instances = [
        "https://searx.be",
        "https://search.inetol.net",
        "https://searx.work",
        "https://searx.tiekoetter.com",
        "https://search.mdosch.de",
    ]
    print("  >> Trying SearXNG meta-search...")
    for q in query_variants[:5]:
        if len(found) >= max_images:
            break
        for base in searx_instances:
            if len(found) >= max_images:
                break
            try:
                api_url = f"{base}/search?q={quote_plus(q)}&categories=images&format=json&safesearch=0&pageno=1"
                resp = session.get(api_url, timeout=20, headers={'Accept': 'application/json'})
                if resp.status_code != 200:
                    continue
                data = resp.json()
                results = data.get('results', [])
                new = 0
                for r in results:
                    u = r.get('img_src') or r.get('thumbnail_src') or ''
                    if u and u not in seen and is_valid_url(u):
                        seen.add(u); found.append(u); new += 1
                        if len(found) >= max_images: break
                print(f"  [SearXNG {base.split('/')[2]} q='{q}'] +{new} (total {len(found)})")
                if new > 0:
                    break  # got results from this instance, try next query variant
            except Exception as e:
                print(f"  [SearXNG {base} q='{q}'] Error: {e}")
                continue

    if len(found) >= max_images:
        return found[:max_images]

    # --- Engine 0.1: Adult image board APIs (Gelbooru, Rule34, Danbooru, Konachan, Yandere) ---
    # These have public JSON APIs, zero filtering, work from any IP
    print("  >> Trying adult image boards (Gelbooru/Rule34/Danbooru)...")
    boards = [
        ("Gelbooru", "https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&limit=100&tags="),
        ("Rule34",   "https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&json=1&limit=100&tags="),
        ("Danbooru", "https://danbooru.donmai.us/posts.json?limit=100&tags="),
        ("Konachan", "https://konachan.com/post.json?limit=100&tags="),
        ("Yandere",  "https://yande.re/post.json?limit=100&tags="),
    ]
    for q in query_variants[:5]:
        if len(found) >= max_images:
            break
        tags = quote_plus(q.replace(' ', '_'))
        for name, base_url in boards:
            if len(found) >= max_images:
                break
            try:
                url = base_url + tags
                resp = session.get(url, timeout=20, headers={'User-Agent': UA, 'Accept': 'application/json'})
                if resp.status_code != 200:
                    continue
                data = resp.json()
                # Gelbooru/Rule34 return list of dicts with 'file_url' or 'sample_url'
                # Danbooru/Konachan/Yandere return list of dicts with 'file_url' or 'large_file_url'
                posts = data if isinstance(data, list) else data.get('post', [])
                new = 0
                for p in posts:
                    if isinstance(p, dict):
                        u = p.get('file_url') or p.get('sample_url') or p.get('large_file_url') or p.get('preview_url') or ''
                        if u and u not in seen and is_valid_url(u):
                            seen.add(u); found.append(u); new += 1
                            if len(found) >= max_images: break
                if new > 0:
                    print(f"  [{name} q='{q}'] +{new} (total {len(found)})")
            except Exception as e:
                print(f"  [{name} q='{q}'] Error: {e}")
                continue

    if len(found) >= max_images:
        return found[:max_images]

    # --- Engine 0.5: Reddit JSON API (include_over_18=on, no filtering) ---
    print(f"  >> Trying Reddit... (have {len(found)} so far)")
    reddit_subs = ['all', 'pics', 'nsfw', 'RealGirls', 'gonewild', 'nsfw_gif',
                   'amateur', 'porn', 'hentai', 'rule34', 'ass', 'boobs',
                   'cumsluts', 'milf', 'teen', 'Asian', 'Latinas', 'Ebony',
                   'Blowjobs', 'anal', 'threesome', 'public', 'creampie']
    for q in query_variants[:5]:
        if len(found) >= max_images:
            break
        for sub in reddit_subs:
            if len(found) >= max_images:
                break
            try:
                reddit_url = f"https://www.reddit.com/r/{sub}/search.json?q={quote_plus(q)}&restrict_sr=on&include_over_18=on&sort=relevance&limit=100"
                resp = session.get(reddit_url, timeout=20, headers={'User-Agent': UA})
                if resp.status_code != 200:
                    continue
                data = resp.json()
                posts = data.get('data', {}).get('children', [])
                new = 0
                for post in posts:
                    p = post.get('data', {})
                    u = p.get('url', '')
                    # Direct image links
                    if u and IMG_EXT_RE.search(u) and u not in seen and is_valid_url(u):
                        seen.add(u); found.append(u); new += 1
                        if len(found) >= max_images: break
                    # Imgur direct image (convert imgur.com/xxx to i.imgur.com/xxx.jpg)
                    if 'imgur.com/' in u and 'i.imgur' not in u:
                        imgur_id = u.rstrip('/').split('/')[-1]
                        if imgur_id and '?' not in imgur_id:
                            direct = f"https://i.imgur.com/{imgur_id}.jpg"
                            if direct not in seen and is_valid_url(direct):
                                seen.add(direct); found.append(direct); new += 1
                                if len(found) >= max_images: break
                    # Reddit preview images
                    preview = p.get('preview', {}).get('images', [])
                    for pv in preview:
                        pu = pv.get('source', {}).get('url', '').replace('&amp;', '&')
                        if pu and pu not in seen and is_valid_url(pu):
                            seen.add(pu); found.append(pu); new += 1
                            if len(found) >= max_images: break
                if new > 0:
                    print(f"  [Reddit r/{sub} q='{q}'] +{new} (total {len(found)})")
            except Exception as e:
                continue

    if len(found) >= max_images:
        return found[:max_images]

    # --- Engine 1: DuckDuckGo i.js API ---
    print("  >> Trying DuckDuckGo API...")
    for q in query_variants:
        if len(found) >= max_images:
            break
        try:
            page_url = f"https://duckduckgo.com/?q={quote_plus(q)}&iax=images&ia=images"
            resp = session.get(page_url, timeout=30)
            html = resp.text
            vqd_match = re.search(r'vqd=["\']([^"\']+)["\']', html)
            if not vqd_match:
                vqd_match = re.search(r'vqd=([a-f0-9-]+)', html)
            if not vqd_match:
                vqd_match = re.search(r'vqd&quot;:&quot;([^&]+)&quot;', html)
            if not vqd_match:
                print(f"  [DDG API q='{q}'] No vqd token found")
                continue
            vqd = vqd_match.group(1)
            s = 0
            while len(found) < max_images:
                params = {'l': 'us-en', 'o': 'json', 'q': q, 'vqd': vqd,
                          'f': ',,,,,', 'p': '1', 'kp': '-1', 's': str(s)}
                api_url = f"https://duckduckgo.com/i.js?{urlencode(params)}"
                try:
                    resp2 = session.get(api_url, timeout=30)
                    data = resp2.json()
                except Exception as e:
                    print(f"  [DDG API q='{q}' s={s}] Error: {e}")
                    break
                results = data.get('results', [])
                if not results:
                    break
                new = 0
                for r in results:
                    u = r.get('image') or r.get('thumbnail') or ''
                    if u and u not in seen and is_valid_url(u):
                        seen.add(u); found.append(u); new += 1
                        if len(found) >= max_images: break
                print(f"  [DDG API q='{q}' s={s}] +{new} (total {len(found)})")
                if new == 0: break
                s += 100
        except Exception as e:
            print(f"  [DDG API q='{q}'] Error: {e}")

    if len(found) >= max_images:
        return found[:max_images]

    # --- Engine 2: DuckDuckGo HTML endpoint ---
    print(f"  >> Trying DDG HTML... (have {len(found)} so far)")
    for q in query_variants:
        if len(found) >= max_images:
            break
        try:
            html_url = f"https://html.duckduckgo.com/html/?q={quote_plus(q)}&kp=-1"
            resp = session.get(html_url, timeout=30)
            html = resp.text
            urls = re.findall(r'(https?://[^\s"\'<>]+\.(?:jpe?g|png|gif|webp|bmp|avif|tiff?)(?:\?[^\s"\'<>]*)?)', html, re.I)
            new = 0
            for u in urls:
                u = u.replace('\\/', '/')
                if u and u not in seen and is_valid_url(u):
                    seen.add(u); found.append(u); new += 1
                    if len(found) >= max_images: break
            print(f"  [DDG HTML q='{q}'] +{new} (total {len(found)})")
        except Exception as e:
            print(f"  [DDG HTML q='{q}'] Error: {e}")

    if len(found) >= max_images:
        return found[:max_images]

    # --- Engine 3: Bing Images with SafeSearch cookies ---
    print(f"  >> Trying Bing Images... (have {len(found)} so far)")
    bing_session = requests.Session()
    bing_session.headers.update(HEADERS)
    bing_session.cookies.set('SRCHHPGUSR', 'ADLT=OFF', domain='.bing.com')
    bing_session.cookies.set('_EDGE_S', 'mkt=en-us&ui=en-us&ADLT=off', domain='.bing.com')
    for q in query_variants:
        if len(found) >= max_images:
            break
        first = 1
        for page in range(1, 8):
            if len(found) >= max_images:
                break
            search_url = (f"https://www.bing.com/images/search?q={quote_plus(q)}"
                          f"&first={first}&count=35&safesearch=off&adlt=off")
            try:
                resp = bing_session.get(search_url, timeout=30)
                html = resp.text
                murls = re.findall(r'&quot;murl&quot;:&quot;(.*?)&quot;', html)
                murls += re.findall(r'"murl":"(.*?)"', html)
                murls += re.findall(r'murl&quot;:&quot;(.*?)&quot;', html)
                new = 0
                for u in murls:
                    u = u.replace('\\/', '/').replace('&amp;', '&')
                    if u and u not in seen and is_valid_url(u):
                        seen.add(u); found.append(u); new += 1
                        if len(found) >= max_images: break
                print(f"  [Bing q='{q}' p{page}] +{new} (total {len(found)})")
                if new == 0: break
                first += 35
            except Exception as e:
                print(f"  [Bing q='{q}' p{page}] Error: {e}")
                break

    if len(found) >= max_images:
        return found[:max_images]

    # --- Engine 4: Direct scrape of image hosting sites ---
    print(f"  >> Trying direct image sites... (have {len(found)} so far)")
    for q in query_variants[:5]:
        if len(found) >= max_images:
            break
        for site in ['https://imgur.com/search?q=', 'https://www.flickr.com/search/?text=']:
            if len(found) >= max_images:
                break
            try:
                url = site + quote_plus(q)
                resp = session.get(url, timeout=30)
                html = resp.text
                urls = re.findall(r'(https?://[^\s"\'<>]+\.(?:jpe?g|png|gif|webp|bmp|avif)(?:\?[^\s"\'<>]*)?)', html, re.I)
                new = 0
                for u in urls:
                    u = u.replace('\\/', '/')
                    if u and u not in seen and is_valid_url(u):
                        seen.add(u); found.append(u); new += 1
                        if len(found) >= max_images: break
                print(f"  [Direct {site.split('/')[2]} q='{q}'] +{new} (total {len(found)})")
            except Exception as e:
                print(f"  [Direct {site} q='{q}'] Error: {e}")

    print(f"  >> Search complete: {len(found)} unique images found")
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
        resp = session.get(url, timeout=30)
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
        print(f"Search mode | query: '{search_query}' | max {max_images} | {workers} workers")
        for u in search_images(search_query, max_images):
            if u not in seen:
                seen.add(u)
                all_imgs.append(u)
    else:
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
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_image, url, output_dir, seen_hashes): url for url in all_imgs}
        for fut in as_completed(futures):
            if fut.result():
                downloaded += 1

    print(f"\nDone! Downloaded {downloaded} unique images to {output_dir}/")


if __name__ == '__main__':
    main()
