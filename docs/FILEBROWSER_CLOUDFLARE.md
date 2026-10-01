# Télécharger les gros fichiers à la demande

Le fichier reste sur Ubuntu. Quand quelqu'un clique sur un lien, Cloudflare transmet la requête au serveur, qui envoie le document directement. Aucun dépôt préalable sur SwissTransfer n'est nécessaire. Ubuntu doit donc être allumé et connecté à Internet pendant tout le téléchargement.

Deux accès sont fournis :

- `/cours` dans Discord produit un lien signé, valable une heure par défaut, pour le fichier sélectionné. Le destinataire de ce lien peut télécharger ce fichier sans compte FileBrowser.
- L'adresse HTTPS du tunnel ouvre FileBrowser après une authentification HTTP avec les identifiants locaux. Cet explorateur permet de consulter et télécharger les documents, sans les modifier.

## Avant de commencer

Terminer [l'installation du bot](UBUNTU_DISCORD.md). Son `.env` doit être rempli et `COURS_DIR` doit exister. Exécuter les commandes suivantes dans `discord_courses`, avec le compte du bot, sans sudo sauf lorsque cela est indiqué.

FileBrowser est archivé : son dépôt annonce l'arrêt des corrections et mises à jour de sécurité. Cette intégration limite les droits et ajoute une authentification indépendante, mais elle ne remplace pas la maintenance du logiciel. Utiliser un Ubuntu maintenu et réserver cet accès aux personnes autorisées. Voir le [dépôt officiel FileBrowser](https://github.com/filebrowser/filebrowser).

## Installer et configurer

```bash
cd ~/isen-cours/discord_courses
./venv/bin/python install_filebrowser.py
./venv/bin/python render_services.py
```

L'installateur récupère les binaires officiels FileBrowser et cloudflared, vérifie leur empreinte SHA-256 puis leur exécution. Les architectures Linux x86_64, i386/i686 et aarch64 sont prévues ; cela ne garantit pas leur compatibilité avec tout ancien noyau. Si un binaire échoue, arrêter ici et lire l'erreur avant d'installer les services.

Il crée une base FileBrowser locale avec un utilisateur `cours` en lecture seule, désactive l'exécution de commandes et écrit dans `.env` :

| Variable | Usage |
| --- | --- |
| `FILEBROWSER_USER` | Identifiant de l'explorateur |
| `FILEBROWSER_PASSWORD` | Mot de passe aléatoire généré, à conserver en privé |
| `COURS_LINK_KEY_FILE` | Fichier local contenant la clé des liens signés |
| `COURS_LINK_URL_FILE` | Fichier local contenant l'adresse du tunnel actif |
| `COURS_LINK_TTL_SECONDS` | Durée des liens signés, 3600 secondes par défaut |

Consulter ces valeurs uniquement sur le serveur dans son éditeur local. Ne jamais envoyer `.env`, la base FileBrowser ou la clé dans GitHub. Relancer l'installateur conserve une configuration qu'il a déjà créée ; une base étrangère est refusée sans modification.

## Démarrer les services

```bash
sudo install -m 644 generated-services/filebrowser-courses.service /etc/systemd/system/
sudo install -m 644 generated-services/discord-downloads.service /etc/systemd/system/
sudo install -m 644 generated-services/discord-download-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now filebrowser-courses discord-downloads discord-download-tunnel
sudo systemctl restart discord-courses
sudo systemctl status filebrowser-courses discord-downloads discord-download-tunnel --no-pager
```

Si le bot est lancé autrement que par le service fourni, redémarrer ce processus avec sa méthode habituelle pour qu'il recharge `.env`.

L'adresse HTTPS courante est dans `download_url.json` :

```bash
./venv/bin/python -c 'import json; print(json.load(open("download_url.json"))["url"])'
```

Le Quick Tunnel ne demande ni domaine ni compte Cloudflare. Son adresse `https://....trycloudflare.com` est temporaire et peut changer au redémarrage. Le lanceur actualise le fichier utilisé par le bot ; les anciens liens ne fonctionnent plus si l'adresse change. Ces tunnels servent au test, sans garantie de disponibilité, avec une limite de 200 requêtes simultanées. Pour une adresse stable, utiliser un tunnel nommé sur son propre domaine et adapter la configuration du lanceur. Voir la [documentation Cloudflare](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

## Architecture et protections

```text
Internet HTTPS → Cloudflare Tunnel
                 → 127.0.0.1:18880 (service Python)
                    ├─ lien signé → document demandé
                    └─ explorateur + mot de passe → 127.0.0.1:18882 (FileBrowser)
```

Les services HTTP écoutent uniquement en local : ne pas ouvrir 18880 ou 18882 sur le routeur. Le proxy contrôle le mot de passe, remplace l'en-tête d'identité et bloque les opérations de modification. FileBrowser possède aussi des permissions de lecture seule. Les liens signés vérifient le chemin, la signature et l'expiration ; ils refusent les chemins sortant du dossier de cours et les liens symboliques.

Un lien signé est un accès au document : celui qui le reçoit peut le télécharger jusqu'à son expiration. Les restrictions de rôles Discord limitent la création de liens dans le bot, mais ne rendent pas les liens déjà distribués personnels.

## Vérifier réellement

1. Ouvrir l'adresse HTTPS depuis un téléphone en données mobiles : sans identifiants, l'explorateur doit demander un mot de passe.
2. Se connecter avec les identifiants FileBrowser locaux et télécharger un petit document.
3. Dans Discord, choisir un fichier dépassant la limite de pièce jointe : cliquer sur le lien puis vérifier le téléchargement et son contenu.
4. Ajouter un fichier sur Ubuntu, cliquer sur Actualiser dans Discord et vérifier qu'il apparaît.

Les tests automatiques vérifient les liens, le streaming HTTP, la pagination et le proxy local. Ils ne prouvent pas à eux seuls qu'un tunnel public, un bot Discord et un serveur Ubuntu sont opérationnels.

## Dépannage

| Problème | Action |
| --- | --- |
| `download_url.json` absent | Consulter `journalctl -u discord-download-tunnel -n 50 --no-pager` |
| HTTP 502 ou tunnel hors ligne | Vérifier les trois services et la connexion Internet d'Ubuntu |
| Ancien lien invalide | Relancer `/cours` pour créer un lien avec l'adresse actuelle |
| HTTP 403 sur un fichier | Lien expiré, signature incorrecte ou fichier non autorisé : créer un nouveau lien |
| HTTP 404 | Fichier déplacé ou supprimé : actualiser le menu |
| Authentification refusée | Vérifier FILEBROWSER_USER/PASSWORD et redémarrer discord-downloads après modification |
| Version Ubuntu trop ancienne | Vérifier Python et les binaires ; migrer vers une version maintenue si nécessaire |

Pour retirer l'accès public :

```bash
sudo systemctl disable --now discord-download-tunnel
```

Cela laisse les cours sur le disque et permet au bot d'envoyer les petits fichiers dans Discord.
