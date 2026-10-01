"""Actualiser automatiquement l'adresse publique à chaque redémarrage du tunnel."""
import json
import os
import re
import signal
import subprocess
from pathlib import Path


def main():
    folder = Path(__file__).resolve().parent
    destination = folder / 'download_url.json'
    destination.unlink(missing_ok=True)
    process = subprocess.Popen([str(folder / 'cloudflared'), 'tunnel', '--no-autoupdate',
                                '--protocol', 'http2', '--url', 'http://127.0.0.1:18880'],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    def stop(signum, frame):
        process.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for line in process.stdout:
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com\b', line)
            if match:
                temporary = destination.with_suffix('.tmp')
                temporary.write_text(json.dumps({'url': match.group(0)}), encoding='utf-8')
                os.replace(str(temporary), str(destination))
            print(line.rstrip(), flush=True)
        return process.wait()
    finally:
        destination.unlink(missing_ok=True)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    raise SystemExit(main())
