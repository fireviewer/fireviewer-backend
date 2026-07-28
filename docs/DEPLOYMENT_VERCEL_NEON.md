# Raccordement Vercel, Neon et Vercel Private Blob

Ce guide raccorde le backend **G1**. Il ne certifie pas un usage opérationnel critique et ne
remplace pas la fermeture des blocages G2 listés dans
[`../../../docs/ADMIN_BACKEND_READINESS.md`](../../../docs/ADMIN_BACKEND_READINESS.md).

## 1. Vérifier localement avec PostGIS

Depuis la racine du dépôt :

```powershell
docker compose up -d database
$env:FV_DATABASE_URL='postgresql+psycopg://fire_viewer:fire_viewer_local_only@localhost:5432/fire_viewer'
& .\.venv\Scripts\alembic.exe upgrade head
& .\.venv\Scripts\uvicorn.exe fire_viewer.main:app --host 127.0.0.1 --port 8000
```

Dans un second terminal :

```powershell
curl.exe -i http://127.0.0.1:8000/readyz
```

Le JSON attendu contient :

```json
{
  "status": "ready",
  "database": "ok",
  "schema_revision": "e5b7c9d2a410",
  "spatial_index": "ok"
}
```

Une migration absente, PostGIS absent ou un index spatial absent doit retourner `503`.

## 2. Créer le projet Neon

1. Créer un projet Neon et conserver la branche principale pour la production.
2. Créer une branche `staging` issue de la branche principale.
3. Dans **Connect**, copier la chaîne directe de la branche pour les migrations.
4. Copier la chaîne poolée pour l'exécution Vercel si elle est proposée.
5. Ne jamais placer ces chaînes dans Git ou dans une variable `VITE_*`.

Neon associe chaque branche à un compute et fournit une chaîne de connexion par branche :
[documentation Neon sur les computes et branches](https://neon.com/docs/manage/endpoints/).

### Appliquer les migrations

```powershell
$env:FV_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST/DATABASE?sslmode=require'
& .\.venv\Scripts\alembic.exe upgrade head
```

Contrôler ensuite dans l'éditeur SQL Neon :

```sql
SELECT version_num FROM alembic_version;
SELECT extversion FROM pg_extension WHERE extname = 'postgis';
SELECT indexname
FROM pg_indexes
WHERE indexname IN (
  'ix_incident_series_reference_geog_gist',
  'ix_incident_series_reference_geom_l93_gist',
  'ix_observation_geometry_geog_gist',
  'ix_observation_geometry_l93_gist'
);
```

## 3. Créer le store Blob privé

Dans le projet Vercel du backend :

1. Ouvrir **Storage**.
2. Choisir **Create Database**, puis **Blob**.
3. Choisir impérativement l'accès **Private**.
4. Connecter le store au projet backend.
5. Vérifier la présence de `BLOB_READ_WRITE_TOKEN` dans les variables du projet si le runtime ne
   fournit pas encore l'authentification OIDC Blob.

La procédure et les versions minimales du SDK sont décrites dans la
[documentation Vercel Private Blob](https://vercel.com/docs/vercel-blob/private-storage).

## 4. Générer l'accès Admin G1

```powershell
& .\.venv\Scripts\fire-viewer-hash-admin-password.exe
```

La commande demande deux fois le mot de passe et affiche une valeur `scrypt$...`. Placer cette
valeur dans `FV_LOCAL_ADMIN_PASSWORD_HASH`. Ne jamais stocker le mot de passe en clair.

## 5. Variables du backend Vercel

Définir séparément les valeurs **Preview** et **Production** :

```text
FV_ENVIRONMENT=production
FV_DATABASE_URL=<connexion Neon poolée>
FV_DATABASE_SCHEMA_REVISION=e5b7c9d2a410
FV_DATABASE_POOL_SIZE=2
FV_DATABASE_MAX_OVERFLOW=3
FV_OBJECT_STORAGE_BACKEND=vercel_blob
FV_OBJECT_STORAGE_PREFIX=firewarning
FV_AUTH_MODE=local_admin
FV_LOCAL_ADMIN_USERNAME=admin
FV_LOCAL_ADMIN_PASSWORD_HASH=<scrypt$...>
FV_PUBLIC_REPORT_HASH_SECRET=<secret aléatoire de 32 caractères minimum>
FV_CORS_ORIGINS=["https://www.exemple.fr"]
FV_TRUSTED_HOSTS=["api.exemple.fr","firewarning-api.vercel.app"]
BLOB_READ_WRITE_TOKEN=<variable injectée par le store>
FV_AGENT_MEDIA_ALLOWED_HOSTS=["private-media.exemple.fr"]
FV_AGENT_EXPECTED_MODEL_REVISIONS={"fire_detection":"<sha-checkpoint>","multimodal_extraction":"<commit-qwen>"}
```

Le mode `local_admin` reste limité à G1. G2 exige OIDC, identités nominatives et MFA.

## 6. Déployer le backend

Configurer la racine du projet Vercel sur `/`. Le fichier
`api/index.py` expose l'application FastAPI ASGI. Vercel détecte les dépendances via
`pyproject.toml`, conformément à la
[documentation du runtime Python](https://vercel.com/docs/functions/runtimes/python).

Les migrations ne doivent pas être lancées au démarrage d'une Function. Le hook de build
`fire_viewer.scripts.migrate_vercel` les exécute avant l'activation d'un déploiement de
production, sous verrou transactionnel PostgreSQL. Un échec de migration fait échouer le build
et conserve le déploiement de production précédent. Les previews ignorent ce hook.

Après le déploiement :

```powershell
curl.exe -i https://api.exemple.fr/healthz
curl.exe -i https://api.exemple.fr/readyz
```

Ne connecter le frontend qu'après un `200` de `/readyz`.

### Dispatcher agentique dans le projet Vercel API

Le projet API utilise trois déclencheurs Vercel :

- suivi du seul job RunPod actif toutes les cinq minutes ;
- preuves utilisateur toutes les trois heures ;
- recherches médiatiques à 11 h et 23 h en heure de Paris. Le run de 11 h inclut la collecte
  satellite, points chauds et thermique.

Les quatre heures UTC du Cron média couvrent le changement heure d'été/heure d'hiver ; la route
refuse silencieusement toute heure locale différente de 11 h ou 23 h. Les incendies actifs sont
mis en file dans un ordre déterministe puis traités un par un. Tant qu'un job RunPod est actif,
aucun second incendie ni aucune autre preuve n'est soumis. Une action humaine peut déclencher
immédiatement le prochain pas autorisé, sans contourner cette exclusion.

Chaque invocation revendique au plus une décision persistée, soumet ou interroge le job RunPod,
enregistre le résultat puis se termine. Il n'existe donc ni boucle résidente ni service CPU
supplémentaire. Le verrou consultatif PostgreSQL, le lease SQL et la barrière `SUBMITTING`
conservent l'exclusion globale et la soumission au plus une fois.

Définir les variables serveur suivantes :

```text
FV_AGENT_DISPATCH_ENABLED=true
CRON_SECRET=<secret aléatoire d'au moins 32 caractères>
FV_AGENT_RUNPOD_TRANSPORT=pod
FV_AGENT_RUNPOD_POD_BASE_URL=<URL HTTPS du pod>
FV_AGENT_RUNPOD_POD_AUTH_TOKEN=<secret serveur>
FV_AGENT_EXECUTION_TIMEOUT_MS=900000
FV_AGENT_JOB_TTL_MS=3600000
FV_AGENT_POLL_INTERVAL_SECONDS=5
FV_AGENT_DISPATCH_LEASE_SECONDS=90
```

Vercel appelle automatiquement les routes sous
`GET /api/v1/internal/agent-orchestrator/*` avec
`Authorization: Bearer $CRON_SECRET`. Le secret RunPod reste uniquement dans les variables du projet
API ; il ne doit être présent ni dans le frontend, ni dans l'image Docker publique du worker.

## 7. Connecter le frontend

Dans le projet Vercel frontend séparé :

```text
VITE_API_BASE_URL=https://api.exemple.fr
```

L'origine doit être HTTPS, sans chemin final. La même origine doit être présente dans
`FV_CORS_ORIGINS` côté API. Les cookies Admin nécessitent des choix de domaine cohérents ; la
configuration la plus simple est `www.exemple.fr` pour le site et `api.exemple.fr` pour l'API.
Avec `SameSite=Strict`, deux domaines de preview Vercel distincts ne constituent pas un
raccordement Admin valide. Utiliser les sous-domaines d'un même domaine enregistré ou un proxy
même origine.

## 8. Upload direct des packages

L'Admin sélectionne directement le dossier produit localement. Le navigateur vérifie
`package-manifest.json`, `catalog.json`, les chemins et les tailles, puis envoie chaque objet au
Blob privé avec le client officiel et le mode multipart. Aucun binaire ne traverse FastAPI :
[guide Blob SDK](https://vercel.com/docs/vercel-blob/using-blob-sdk).

Le parcours implémenté est :

1. émission serveur d'un jeton client limité à `packages/{upload_id}/` ;
2. upload direct et multipart depuis le navigateur ;
3. finalisation légère avec la liste des objets ;
4. contrôle serveur par `head()` de la présence, taille et type ;
5. enregistrement atomique du package en brouillon ;
6. validation, preview privée et publication explicites.

NON VÉRIFIÉ dans ce dépôt : un envoi réel du package de 417 Mo vers le store Vercel de production.

## 9. Vérifications de raccordement

- `/readyz` retourne la révision attendue et `spatial_index=ok`.
- Une connexion Admin pose uniquement `fireviewer_admin` en `Secure; HttpOnly; SameSite=Strict`.
- `/api/v1/admin/session` renvoie `csrf_token`; aucune valeur de session n'est dans le stockage du
  navigateur.
- Une mutation sans `X-CSRF-Token` retourne `403`.
- Les réponses Admin portent `Cache-Control: no-store`.
- Un asset privé ne peut pas être lu sans l'API ou un mécanisme signé autorisé.
- Une nouvelle migration rend `/readyz` indisponible tant que
  `FV_DATABASE_SCHEMA_REVISION` n'est pas synchronisée.
