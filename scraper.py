#!/usr/bin/env python3
import os
import sys
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

def is_valid_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except:
        return False

def get_image_urls(url, max_images=50):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        img_tags = soup.find_all('img')
        image_urls = []
        for img in img_tags:
            src = img.get('src') or img.get('data-src')
            if src:
                absolute_url = urljoin(url, src)
                if is_valid_url(absolute_url):
                    image_urls.append(absolute_url)
                    if len(image_urls) >= max_images:
                        break
        return image_urls
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return []

def download_image(url, output_dir, index):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        content_type = response.headers.get('content-type', '')
        if 'jpeg' in content_type or 'jpg' in content_type:
            ext = '.jpg'
        elif 'png' in content_type:
            ext = '.png'
        elif 'gif' in content_type:
            ext = '.gif'
        elif 'webp' in content_type:
            ext = '.webp'
        elif 'svg' in content_type:
            ext = '.svg'
        else:
            parsed = urlparse(url)
            ext = os.path.splitext(parsed.path)[1] or '.jpg'
        filename = f"image_{index:04d}{ext}"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, 'wb') as f:
            f.write(response.content)
        print(f"Downloaded: {filename}")
        return True
    except Exception as e:
        print(f"Error downloading {url}: {e}")
        return False

def main():
    urls_str = os.getenv('URLS', '')
    max_images = int(os.getenv('MAX_IMAGES', '50'))
    if not urls_str:
        print("Error: No URLs provided")
        sys.exit(1)
    urls = [url.strip() for url in urls_str.split(',') if url.strip()]
    if not urls:
        print("Error: No valid URLs provided")
        sys.exit(1)
    output_dir = 'downloaded_images'
    os.makedirs(output_dir, exist_ok=True)
    print(f"Scraping {len(urls)} URL(s) for images...")
    print(f"Max images per URL: {max_images}")
    total_downloaded = 0
    for url_idx, url in enumerate(urls, 1):
        print(f"\n[{url_idx}/{len(urls)}] Processing: {url}")
        image_urls = get_image_urls(url, max_images)
        print(f"Found {len(image_urls)} image(s)")
        for img_idx, img_url in enumerate(image_urls, 1):
            print(f"  [{img_idx}/{len(image_urls)}] Downloading...")
            if download_image(img_url, output_dir, total_downloaded + 1):
                total_downloaded += 1
    print(f"\nTotal images downloaded: {total_downloaded}")
    print(f"Images saved to: {output_dir}/")

if __name__ == '__main__':
    main()
