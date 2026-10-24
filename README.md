# Actor Lists – filmographie d'acteur synchronisée automatiquement

## Ce que fait ce projet

Chaque jour (via GitHub Actions), le script :

1. récupère la filmographie (films uniquement) d'un acteur sur **TMDB** ;
2. filtre les petits rôles, caméos, documentaires, etc. ;
3. met à jour une **liste statique MDBList** (ajoute les nouveaux films, retire ceux qui ne correspondent plus) ;
4. génère des images de l'acteur (bannière paysage, affiche portrait, photo brute) dans le dossier `banners/`.

La liste MDBList est ensuite lue par l'addon **AIOMetadata** (Stremio) et les images servent d'illustration de catalogue dans **Omni Media Hub**.

## Prérequis

- Un compte **GitHub** (gratuit).
- Une clé API **TMDB** : https://www.themoviedb.org/settings/api (compte gratuit, choisir « Developer »). Vous pouvez utiliser soit la « Clé API » (courte), soit le « Jeton d'accès en lecture » (long, commence par `eyJ`).
- Un compte **MDBList** et sa clé API : https://mdblist.com/preferences (section « API Key »).

## Étape 1 – Créer la liste statique sur MDBList

1. Sur https://mdblist.com, allez dans **My Lists** puis **Create new list** (ou « Add List »).
2. Donnez un nom (ex. `Keanu Reeves`) et choisissez le type **Static list** (liste statique, pas dynamique).
3. C'est tout : le script retrouve l'identifiant de la liste tout seul à partir de son nom (majuscules et accents ignorés). Par défaut il cherche une liste qui porte **le nom de l'acteur** ; si votre liste s'appelle autrement, indiquez son nom dans `mdblist_list_name`.
   - Si besoin, vous pouvez voir vos listes et leurs identifiants en ouvrant (en remplaçant `VOTRE_CLE`) `https://api.mdblist.com/lists/user?apikey=VOTRE_CLE`, puis forcer un identifiant avec `mdblist_list_id`.

## Étape 2 – Créer le dépôt GitHub

1. Sur GitHub : **New repository**. Nom au choix (ex. `actor-lists`). Choisissez **Public** (nécessaire pour que les liens d'images `raw.githubusercontent.com` fonctionnent).
2. Envoyez-y tous les fichiers de ce dossier (**Add file → Upload files**, ou `git push`). Veillez à bien inclure le dossier caché `.github/workflows/sync.yml`. Si l'envoi par glisser-déposer ignore les dossiers cachés, créez le fichier à la main : **Add file → Create new file**, nom `.github/workflows/sync.yml`, puis collez le contenu.

## Étape 3 – Ajouter les secrets

Dans le dépôt : **Settings → Secrets and variables → Actions → New repository secret**. Créez deux secrets :

| Nom | Valeur |
| --- | --- |
| `TMDB_API_KEY` | votre clé TMDB |
| `MDBLIST_API_KEY` | votre clé MDBList |

Ne mettez **jamais** ces clés dans `config.json`.

## Étape 4 – Modifier `config.json`

Ouvrez `config.json` (bouton crayon sur GitHub) et remplacez les valeurs. JSON n'accepte pas de commentaires, voici donc la signification de chaque champ :

| Champ | Rôle |
| --- | --- |
| `name` | Nom de l'acteur (affiché sur les images). |
| `tmdb_id` | Identifiant TMDB de la personne (le nombre dans l'URL `themoviedb.org/person/6384-keanu-reeves`). Facultatif : s'il manque, le script cherche le nom sur TMDB et prend le résultat le plus populaire (il l'affiche dans les logs, vérifiez-le). |
| `mdblist_list_name` | Nom de votre liste MDBList. Facultatif : par défaut, le nom de l'acteur. Le script retrouve l'identifiant tout seul. |
| `mdblist_list_id` | Facultatif : identifiant numérique de la liste, prioritaire sur le nom si vous le renseignez. |
| `max_cast_order` | Ne garde que les films où l'acteur est dans les N premiers du générique (ordre TMDB). Évite les caméos. `null` = pas de limite. |
| `min_vote_count` | Ignore les films avec moins de N votes sur TMDB (films obscurs). `0` = tout garder. |
| `exclude_genre_ids` | Genres exclus (par numéro TMDB). `99` = Documentaire. |
| `include_upcoming` | `true` = inclut les films pas encore sortis (ou sans date). |
| `exclude_tmdb_ids` | Films à toujours exclure, par identifiant TMDB (ex. `[603]`). |
| `include_tmdb_ids` | Films à toujours ajouter, même s'ils sont filtrés. |

Dans tous les cas, les rôles « Himself/Self » (apparitions en tant que soi-même) et les films pour adultes sont exclus.

Chaque modification de `config.json` déclenche automatiquement une synchronisation.

## Étape 5 – Lancer le workflow manuellement

Onglet **Actions** du dépôt → **Sync actor lists** → **Run workflow**. Si c'est la première fois, GitHub peut demander d'activer les workflows : acceptez. Au bout d'une minute environ, le journal affiche la liste des films, ce qui a été ajouté ou retiré, et un commit automatique apparaît avec les images dans `banners/`.

Ensuite, le workflow tourne tout seul chaque jour vers 05:17 UTC.

## Étape 6 – Ajouter la liste MDBList dans AIOMetadata

1. Ouvrez la page de configuration de votre instance **AIOMetadata**.
2. Allez dans la section **MDBList**, saisissez votre **clé API MDBList**.
3. Ajoutez la liste : sélectionnez-la dans vos listes (« user lists ») ou ajoutez-la par son URL.
4. Enregistrez la configuration, puis **réinstallez l'addon** dans Omni (avec le nouveau lien de manifeste) pour que le nouveau catalogue apparaisse.

## Étape 7 – Mettre l'image dans Omni

Les images sont disponibles à ces adresses (remplacez `<user>`, `<repo>` et `<slug>` ; le slug est le nom en minuscules sans accents, avec des tirets, ex. `keanu-reeves`) :

- Paysage : `https://raw.githubusercontent.com/<user>/<repo>/main/banners/<slug>.jpg`
- Portrait : `https://raw.githubusercontent.com/<user>/<repo>/main/banners/<slug>_poster.jpg`

Dans Omni Media Hub, cherchez l'option de modification du catalogue / de la collection et un champ image / couverture, puis collez le lien. L'emplacement exact du menu peut varier selon la version de l'application.

## Ajouter un autre acteur

Dans `config.json`, ajoutez un second bloc dans la liste `actors` (séparé par une virgule) :

```json
{
  "actors": [
    { "name": "Keanu Reeves", "tmdb_id": 6384 },
    { "name": "Sandra Bullock", "mdblist_list_name": "Films de Sandra" }
  ]
}
```

Les champs omis prennent leurs valeurs par défaut (`max_cast_order` 15, `min_vote_count` 20, documentaires exclus, films à venir inclus). Créez d'abord une nouvelle liste statique MDBList pour chaque acteur (étape 1), puis répétez les étapes 6 et 7.

## Dépannage

- **« TMDB_API_KEY est absente »** : le secret n'existe pas ou son nom est mal orthographié (étape 3).
- **TMDB 401** : clé invalide. Recopiez-la sans espace.
- **MDBList 401/403 ou « liste introuvable »** : mauvaise clé, ou `mdblist_list_id` incorrect / liste qui n'est pas la vôtre. Revérifiez avec l'adresse `/lists/user` (étape 1).
- **« Aucune liste MDBList nommée … »** : le nom ne correspond à aucune de vos listes ; le message affiche vos listes existantes. Renommez la liste ou corrigez `mdblist_list_name`.
- **« Plusieurs listes MDBList nommées … »** : deux listes ont le même nom ; renommez-en une ou indiquez `mdblist_list_id`.
- **Le workflow échoue au moment du push** : vérifiez **Settings → Actions → General → Workflow permissions** et choisissez « Read and write permissions ».
- **Mauvais acteur trouvé par la recherche** : renseignez `tmdb_id` explicitement.
- **Un film manque ou est en trop** : utilisez `include_tmdb_ids` / `exclude_tmdb_ids`, ou ajustez `max_cast_order` et `min_vote_count`.
- **L'image ne s'affiche pas dans Omni** : le dépôt doit être public, la branche doit s'appeler `main`, et le workflow doit avoir tourné au moins une fois.
- **Tester en local** : `pip install -r requirements.txt`, définissez `TMDB_API_KEY` (et `MDBLIST_API_KEY`), puis `python sync.py --dry-run` (affiche la liste et les différences sans rien écrire). Options : `--actor "Nom"`, `--no-banner`. Tests : `python -m unittest discover -s tests`.
