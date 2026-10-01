"""Vérifier le catalogue et les menus sur disque, sans connexion à Discord."""
import asyncio
import json
from pathlib import Path
from dotenv import load_dotenv
from course_browser import Catalog, CourseView, Settings


async def main():
    load_dotenv(Path(__file__).with_name('.env'))
    catalog = Catalog(Settings.from_env())
    pending = [Path('.')]
    folders = files = paginated = 0
    while pending:
        relative = pending.pop()
        view = CourseView(catalog, 1)
        view.relative = relative
        await view.build()
        entries = catalog.list(relative)
        folders += 1
        for entry in entries:
            if entry.folder:
                pending.append(entry.path)
            else:
                files += 1
        if len(entries) > 25:
            paginated += 1
            view.page = 1
            await view.build()
            assert len(view.children[0].options) <= 25
        view.stop()
    print(json.dumps({'root': str(catalog.root), 'courses': len(catalog.list()),
                      'folders_checked': folders, 'documents_visible': files,
                      'folders_paginated': paginated}, ensure_ascii=True))


if __name__ == '__main__':
    asyncio.run(main())
