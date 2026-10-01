# Configurer Ubuntu et le bot Discord

## Préparer les fichiers et le venv

Installer le projet dans un dossier sans espaces, par exemple `~/isen-cours`, et installer Python 3.10+ et son module venv sur une distribution maintenue. Un ancien serveur peut réutiliser un Python 3.8 compatible déjà installé, mais pas un Python 3.6.

```bash
cd ~/isen-cours/discord_courses
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
chmod 600 .env
nano .env
```

Créer le dossier des cours avec son propre compte, ou faire attribuer les droits nécessaires par l'administrateur. `COURS_DIR` doit être absolu et correspondre exactement au `UBUNTU_REMOTE_DIR` de la synchronisation Windows.

## Configurer Discord

Dans le Developer Portal Discord, créer une application/bot ou utiliser son bot existant. Copier son token uniquement dans `.env`, sans le committer ni le publier dans un salon.

```dotenv
DISCORD_TOKEN=
DISCORD_GUILD_ID=
COURS_DIR=/home/ton_utilisateur/cours
COURS_VIEW_TIMEOUT=1800
COURS_ALLOWED_ROLE_IDS=
```

Renseigner `DISCORD_GUILD_ID` avec l'ID du serveur pour y enregistrer immédiatement `/cours`. Vide : enregistrement global, dont l'apparition peut prendre plus de temps. Pour obtenir les IDs, activer le mode développeur dans Discord et copier l'identifiant du serveur ou du rôle.

Inviter le bot avec les scopes `bot` et `applications.commands`. Accorder Voir le salon, Envoyer des messages, Intégrer des liens et Joindre des fichiers. Pour un fil de discussion, autoriser aussi les messages dans les fils. Aucun intent privilégié Message Content n'est nécessaire pour `/cours`.

```bash
./venv/bin/python check_courses.py
./venv/bin/python bot.py
```

Tester ensuite le menu dans Discord et l'envoi d'un petit PDF. `COURS_ALLOWED_ROLE_IDS` peut limiter l'accès à des IDs de rôles séparés par des virgules ; vide, tous les membres du serveur peuvent lancer la commande.

## Brancher sur un bot existant

Copier `course_browser.py` et `download_links.py` à côté du bot existant, installer les dépendances, charger son `.env` puis ajouter dans son `setup_hook` :

```python
await self.load_extension('course_browser')
```

Conserver la stratégie de synchronisation `bot.tree.sync()` déjà utilisée par le bot. Charger l'extension une seule fois et ne pas lancer deux instances avec le même token. Le `bot.py` fourni est un lanceur autonome : il ne reprend pas automatiquement les autres fonctions d'un bot existant.

## Lancer au démarrage avec systemd

Les fichiers `.service` sont des modèles. Générer ceux adaptés au compte courant et au dossier du projet :

```bash
./venv/bin/python render_services.py
sudo install -m 644 generated-services/discord-courses.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discord-courses
sudo journalctl -u discord-courses -n 30 --no-pager
```

Lancer `render_services.py` avec le compte qui doit exécuter le bot, sans `sudo`. Ne pas installer les modèles contenant `CHANGE_ME_USER` directement. Le dossier du venv doit s'appeler `venv`.

## Erreurs courantes

| Symptôme | Vérification |
| --- | --- |
| `/cours` n'apparaît pas | Bot invité, scope applications.commands, bon DISCORD_GUILD_ID et synchronisation réussie |
| Connexion refusée | Token valide dans le `.env` lu par le processus |
| Dossier introuvable | COURS_DIR absolu, même dossier que le transfert SFTP |
| Permissions insuffisantes | Le compte Linux peut lire les fichiers et traverser chaque dossier parent |
| Pièce jointe refusée | Permission Joindre des fichiers et limite réelle Discord |
| Boutons désactivés | Menu expiré : relancer `/cours` |
| Nouveau fichier absent | Actualiser, puis vérifier que le transfert vers Ubuntu est terminé |

Les dossiers techniques, fichiers cachés, liens symboliques, transferts incomplets et fichiers spéciaux sont exclus des menus.
