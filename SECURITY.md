# Identifiants et partage du projet

Ce dépôt partage du code et des exemples vides. Les identifiants doivent être créés et conservés séparément par chaque utilisateur.

Ne jamais committer `.env`, tokens Discord ou GitHub, mots de passe Junia/SSH, clés privées, session Chromium, base FileBrowser, clé de signature ou liens privés de téléchargement. Ne jamais partager un `.env` complet pour donner accès aux cours : il peut contenir le token du bot et des accès au serveur.

Les `.gitignore` ne retirent pas un secret déjà committé. Si un secret est envoyé dans GitHub ou dans une discussion, le révoquer auprès du service concerné et créer un nouvel accès ; effacer simplement le fichier ne suffit pas.

Sur Ubuntu, donner à `.env` et `.download_key` les permissions `600`. Le bot et les services s'exécutent avec un compte non root. Ne pas désactiver la vérification des clés SSH.

Le dossier de cours ne doit pas contenir les fichiers de configuration du bot. Les liens directs signés autorisent toute personne qui possède le lien à télécharger le document jusqu'à son expiration. Choisir des salons Discord et des destinataires adaptés.

FileBrowser original est archivé et ne reçoit plus de correctifs : conserver l'authentification indépendante de la passerelle, les services sur localhost, le compte FileBrowser non administrateur, le runner désactivé et la lecture seule. Le tunnel HTTPS ne remplace pas une authentification ni les mises à jour du serveur.

Avant tout envoi Git : vérifier `git status`, `git diff --cached` et les fichiers réellement suivis. Les fichiers de cours et inventaires personnels ne font pas partie de ce dépôt.
