import requests
from bs4 import BeautifulSoup
import os
import re
import time

client_id_cache = None
SEARCH_TIMEOUT = 120

def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": 'https://soundcloud.com/',
        "Upgrade-Insecure-Requests": "1"
    }

def get_client_id():
    global client_id_cache
    if client_id_cache:
        return client_id_cache
    try:
        res = requests.get('https://soundcloud.com/', headers=get_headers(), timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        script_tags = soup.find_all('script', {'crossorigin': True})
        urls = [tag.get('src') for tag in script_tags if tag.get('src') and tag.get('src').startswith('https')]
        if not urls:
            raise Exception('Không tìm thấy URL script')
        res = requests.get(urls[-1], headers=get_headers(), timeout=15)
        res.raise_for_status()
        match = re.search(r'client_id:"(.*?)"', res.text)
        if not match:
            raise Exception('Không tìm thấy client_id')
        client_id_cache = match.group(1)
        return client_id_cache
    except Exception as e:
        print(f'[scll] get_client_id error: {e}')
        return None

def wait_for_client_id(max_tries=5):
    for _ in range(max_tries):
        client_id = get_client_id()
        if client_id:
            return client_id
        time.sleep(2)
    return None

def search_songs(query):
    try:
        base_url = 'https://soundcloud.com'
        search_url = f'https://m.soundcloud.com/search?q={requests.utils.quote(query)}'
        response = requests.get(search_url, headers=get_headers(), timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        songs = []
        url_pattern = re.compile(r'^/[^/]+/[^/]+$')
        for element in soup.select('li > div'):
            a_tag = element.select_one('a')
            if a_tag and a_tag.has_attr('href'):
                relative_url = a_tag['href']
                if url_pattern.match(relative_url):
                    title = a_tag.get('aria-label', '').strip()
                    link = base_url + relative_url
                    img_tag = element.select_one('img')
                    cover_url = img_tag['src'] if (img_tag and img_tag.has_attr('src')) else ""
                    songs.append((link, title, cover_url))
            if len(songs) >= 10:
                break
        return songs
    except Exception as e:
        print(f'[scll] search_songs error: {e}')
        return []

def get_music_stream_url(link):
    try:
        client_id = wait_for_client_id()
        if not client_id:
            return None
        api_url = f'https://api-v2.soundcloud.com/resolve?url={link}&client_id={client_id}'
        response = requests.get(api_url, headers=get_headers(), timeout=15)
        response.raise_for_status()
        data = response.json()
        for transcode in data.get('media', {}).get('transcodings', []):
            if transcode['format']['protocol'] == 'progressive':
                stream_url = transcode['url']
                stream_response = requests.get(
                    f"{stream_url}?client_id={client_id}",
                    headers=get_headers(), timeout=15
                )
                stream_response.raise_for_status()
                return stream_response.json().get('url')
        return None
    except Exception as e:
        print(f'[scll] get_music_stream_url error: {e}')
        return None

def get_track_cover(link):
    try:
        client_id = wait_for_client_id()
        if not client_id:
            return None
        api_url = f'https://api-v2.soundcloud.com/resolve?url={link}&client_id={client_id}'
        response = requests.get(api_url, headers=get_headers(), timeout=15)
        response.raise_for_status()
        data = response.json()
        cover_url = data.get("artwork_url")
        if cover_url:
            cover_url = cover_url.replace('-large', '-t500x500')
        else:
            cover_url = data.get("user", {}).get("avatar_url", "")
            if cover_url:
                cover_url = cover_url.replace('-large', '-t500x500')
        return cover_url
    except Exception as e:
        print(f'[scll] get_track_cover error: {e}')
        return None
