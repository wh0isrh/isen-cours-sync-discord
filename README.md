# Cours ISEN : Moodle → Ubuntu → Discord → téléchargement HTTPS

Ce projet permet de récupérer les supports accessibles sur Junia Learning depuis Windows, de les ranger sur un serveur Ubuntu et de les consulter avec une commande Discord `/cours`. Pour les gros fichiers, le bot peut créer un lien HTTPS temporaire, sans uploader le document chez un autre hébergeur.

Le dépôt contient uniquement du code, des tests et des exemples de configuration. Il ne contient aucun cours, mot de passe, token Discord, profil navigateur ou adresse réelle du serveur d'origine. Chaque personne doit configurer ses propres accès.

## Les trois briques

| Brique | Où elle tourne | Ce qu'elle fait |
| --- | --- | --- |
| `moodle_sync/` | Windows | Connexion Microsoft visible avec A2F, téléchargement en flux et transfert SFTP vers Ubuntu |
| `discord_courses/` | Ubuntu | Menus Matière → Chapitre → Fichier, Retour, Actualiser et pagination |
| FileBrowser + passerelle + Cloudflare Tunnel | Ubuntu, optionnel | Consultation web protégée et liens directs temporaires pour les gros documents |

Les cours sont rangés comme ceci :

```text
COURS_DIR/
├── Automatique/
│   ├── Chapitre 1/
│   │   └── cours.pdf
│   └── TD/
│       └── exercices.pdf
└── Electronique/
    └── Semaine 1/
        └── diaporama.ppsx
```

## Par où commencer ?

- Tu as déjà les fichiers sur Ubuntu : commence par le bot Discord.
- Tu veux récupérer les cours depuis Moodle : commence par la synchronisation Windows.
- Tu veux télécharger les gros fichiers depuis n'importe où : ajoute la partie HTTPS après avoir vérifié que le bot fonctionne.

Guides détaillés : [Windows et Moodle](docs/WINDOWS_MOODLE.md), [bot et serveur Ubuntu](docs/UBUNTU_DISCORD.md), [FileBrowser et liens HTTPS](docs/FILEBROWSER_CLOUDFLARE.md).

## Démarrage rapide du bot

Sur un Ubuntu disposant de Python 3.10 ou plus récent :

```bash
git clone https://github.com/wh0isrh/isen-cours-sync-discord.git ~/isen-cours
cd ~/isen-cours/discord_courses
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
nano .env
```

Renseigner au minimum `DISCORD_TOKEN`, `COURS_DIR` et de préférence `DISCORD_GUILD_ID`. Ne jamais mettre le token dans `bot.py` ou dans GitHub.

```bash
./venv/bin/python check_courses.py
./venv/bin/python bot.py
```

Puis lancer `/cours` dans le serveur Discord où le bot est invité, ou en message privé avec le bot. En DM, l'accès reste réservé aux membres autorisés d'un serveur du bot ; les rôles configurés sont vérifiés sur ce serveur. Le bot doit disposer des permissions Voir le salon, Envoyer des messages, Intégrer des liens et Joindre des fichiers. L'invitation doit inclure les scopes `bot` et `applications.commands`.

## Comportement de l'explorateur

1. `/cours` ouvre le menu des matières.
2. Choisir une matière puis un chapitre.
3. Choisir le document : le bot l'envoie dans le salon, ou fournit un lien si le fichier dépasse la limite autorisée.

Le menu affiche jusqu'à 25 choix par page, à tous les niveaux. Les boutons permettent de revenir au parent, changer de page ou relire le disque. Les underscores deviennent des espaces à l'affichage ; le fichier conserve son vrai nom dans la pièce jointe. Les noms de plus de 100 caractères sont abrégés dans le menu uniquement.

Chaque explorateur est réservé à la personne qui l'a ouvert. Les documents envoyés sont visibles par les membres du salon. Le menu expire après 30 minutes sans clic ; les menus ouverts avant un redémarrage du bot doivent être rouverts avec `/cours`.

Le bot utilise le minimum entre son plafond de 25 Mio et la limite annoncée par Discord pour l'interaction. Sans hébergement HTTPS configuré, il indique la taille et le chemin Ubuntu du gros fichier. Ce chemin n'est pas un lien Internet.

## Limites à connaître

- Ubuntu doit être allumé et connecté pour le bot et pour les téléchargements directs.
- Le tunnel temporaire `trycloudflare.com` change d'adresse au redémarrage. Les anciens liens cessent alors de fonctionner ; choisir à nouveau le fichier dans Discord crée un nouveau lien.
- Le projet FileBrowser original est archivé : le montage fourni ajoute une authentification indépendante et la lecture seule. Voir le guide avant toute exposition Internet.
- Les IDs des 13 cours dans `moodle_sync/sync_moodle.py` correspondent à ISEN Lille 2026/2027. Adapter `COURSES` pour une autre promotion ; un ID connu ne donne pas accès à un cours protégé.
- Les fichiers supprimés localement ou sur Moodle ne sont pas automatiquement supprimés d'Ubuntu. Ce choix évite une perte accidentelle.
- Sur un ancien Ubuntu, utiliser un Python compatible dans un venv. Le Python système 3.6 ne convient pas au bot ; les composants Discord ont été conçus pour Python 3.8+, tandis que la synchronisation Windows exige Python 3.10+.

## Tests

Dans un environnement de développement installé avec les deux `requirements.txt` :

```bash
python -m pip install -r moodle_sync/requirements.txt -r discord_courses/requirements.txt
python -m playwright install chromium
python -m unittest discover -s moodle_sync -v
python -m unittest discover -s discord_courses -v
```

Les tests ne contactent ni Junia ni ton serveur et ne publient aucun message Discord. Ils utilisent des serveurs HTTP/SFTP locaux et des interactions simulées. Les tests spécifiques aux liens symboliques/FIFO sont exécutés sur Linux.

La préparation locale des scripts et les tests ne prouvent pas que le tunnel est déployé : vérifier un vrai téléchargement HTTPS après installation sur son propre serveur.

## Partager ce dépôt privé avec un copain

Dans GitHub : Settings → Collaborators / Manage access → Add people. Inviter son compte GitHub avec l'accès minimal souhaité. Un lien vers un dépôt privé ne lui donne pas automatiquement accès. Partager les instructions ; il crée son propre `.env`. Ne pas lui envoyer celui d'une autre installation.

Voir aussi [les règles pour les secrets](SECURITY.md).
