# Installer la récupération des cours sur Windows

## 1. Préparer Python

Installer Python 3.10 ou plus récent avec le lanceur `py`, puis cloner le dépôt. Dans le dossier `moodle_sync`, double-cliquer sur `setup_windows.bat`. Ce fichier crée le venv, installe les dépendances et Chromium, puis crée `.env` s'il n'existe pas.

Équivalent PowerShell :

```powershell
cd "CHEMIN_DU_DEPOT\moodle_sync"
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
if (!(Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

## 2. Renseigner sa configuration

```dotenv
JUNIA_EMAIL=
JUNIA_PASSWORD=
WAIT_2FA_SECONDS=15
UBUNTU_HOST=ADRESSE_DE_TON_SERVEUR
UBUNTU_PORT=22
UBUNTU_USER=ton_utilisateur
UBUNTU_PASSWORD=
UBUNTU_SSH_KEY_PATH=
UBUNTU_REMOTE_DIR=/home/ton_utilisateur/cours
LOCAL_ONLY=false
```

Les valeurs de l'exemple sont à remplacer. `192.0.2.10`, présent dans `.env.example`, est une adresse de documentation : elle ne correspond à aucun serveur configuré.

Laisser les identifiants Junia vides permet une connexion manuelle dans Chromium visible. Sinon, le script remplit les champs Microsoft connus. L'A2F reste à valider sur le téléphone : le compte à rebours ne remplace pas la vérification de connexion réelle.

SSH accepte un mot de passe ou une clé privée. Au premier accès, comparer l'empreinte SSH affichée avec celle du serveur avant de l'approuver. Ne pas désactiver cette vérification.

## 3. Lancer la synchronisation

Double-cliquer sur `sync_moodle.bat`. Pour avoir le lanceur sur le Bureau, créer un raccourci vers ce fichier ; déplacer le batch seul empêcherait de retrouver le projet.

Le script se connecte en SFTP au démarrage, ouvre le navigateur, parcourt les cours configurés puis télécharge et envoie leurs ressources. Les répertoires distants sont créés au besoin. Les tailles et les empreintes SHA-256 servent à vérifier les transferts et éviter les doublons. Les temporaires sont supprimés après succès ; les originaux locaux restent conservés.

Les liens externes et les activités interactives sont consignés dans des fichiers Markdown. Le script ne répond pas aux quiz et ne contourne pas les clés d'inscription.

## 4. Respecter une sélection de fichiers déjà conservés

Si tu disposes d'un dossier local de cours trié, renseigner `AUDIT_DIR` avec son chemin et mettre `LOCAL_ONLY=true`. Le script envoie uniquement les fichiers présents et n'ouvre pas Moodle. Il ne récupère donc pas les fichiers retirés de ce dossier.

```powershell
.\.venv\Scripts\python.exe sync_moodle.py --local-only
```

`--moodle` relance explicitement la découverte, même si `LOCAL_ONLY=true`. Le fichier `audited_files.json` du dépôt est vide volontairement : aucun inventaire personnel n'est partagé. Il n'est pas nécessaire pour une récupération fraîche ou pour le mode local.

## 5. Réveil réseau et rapports

`WOL_MAC` peut contenir la MAC de la carte Ethernet du serveur, avec `WOL_BROADCAST` adapté au réseau local. Le Wake-on-LAN doit être pris en charge et activé sur la machine. Être connecté au même réseau ou disposer d'un relais adapté est nécessaire ; renseigner une IP ne rend pas un serveur privé accessible depuis Internet.

Les rapports sont dans `.state/sync.log` et `.state/dernier_rapport.json`. La fenêtre du batch reste ouverte à la fin. `.state/` contient aussi la session navigateur et doit rester hors du dépôt.
