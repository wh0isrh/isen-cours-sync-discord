"""Liens de téléchargement HMAC à durée limitée, Python 3.8+."""
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit


class SignedLinks:
    def __init__(self, key_file, url_file, ttl=3600):
        self.key_file, self.url_file = Path(key_file), Path(url_file)
        self.ttl = ttl

    def signature(self, relative, expires):
        key = self.key_file.read_bytes()
        if len(key) < 32:
            raise ValueError('Clé de téléchargement trop courte')
        payload = json.dumps([relative, int(expires)], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    def verify(self, relative, expires, signature):
        try:
            expires = int(expires)
            now = int(time.time())
            return now < expires <= now + self.ttl + 60 and hmac.compare_digest(self.signature(relative, expires), signature)
        except (ValueError, TypeError, OSError):
            return False

    def url(self, relative):
        base = json.loads(self.url_file.read_text(encoding='utf-8'))['url'].rstrip('/')
        parsed = urlsplit(base)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError('Adresse HTTPS de téléchargement invalide')
        expires = int(time.time()) + self.ttl
        return base + '/download?' + urlencode({'path': relative, 'expires': expires,
                                               'sig': self.signature(relative, expires)})
