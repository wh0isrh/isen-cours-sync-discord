"""Générer les services systemd pour le compte et les dossiers de cette installation."""
import os
import pwd
from pathlib import Path
from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent


def main():
    if os.geteuid() == 0:
        raise SystemExit('Lancer ce script avec le compte du bot, sans sudo.')
    if any(c.isspace() for c in str(BASE)):
        raise SystemExit('Installer le projet dans un dossier sans espaces pour ces modèles systemd.')
    root = dotenv_values(BASE / '.env').get('COURS_DIR', '')
    if not root or not Path(root).is_absolute() or any(c in root for c in '\n\r"'):
        raise SystemExit('Renseigner COURS_DIR absolu dans .env.')
    username = pwd.getpwuid(os.geteuid()).pw_name
    output = BASE / 'generated-services'
    output.mkdir(exist_ok=True)
    for template in BASE.glob('*.service'):
        text = template.read_text(encoding='utf-8')
        text = text.replace('CHANGE_ME_USER', username).replace('/opt/isen-cours/discord_courses', str(BASE))
        text = text.replace('ReadOnlyPaths=/srv/cours', 'ReadOnlyPaths="' + root + '"')
        (output / template.name).write_text(text, encoding='utf-8')
    print('Services générés dans', output)


if __name__ == '__main__':
    main()
