# FireViewer — backend événementiel et incident-centrique

Ce dépôt porte le registre durable des incidents, des contributions événementielles, des preuves,
des tentatives de localisation, des événements d'activité, des sources externes et des décisions
de revue ou de publication. La refonte v2 est **additive** : les contrats `/api/v1` restent
disponibles pendant la migration et ne sont pas convertis artificiellement en événements complets.

La doctrine produit et les contrats transverses sont maintenus dans le dépôt canonique
[`fireviewer/Fireviewer_doc`](https://github.com/fireviewer/Fireviewer_doc). Les documents présents
dans [`docs/`](docs/) détaillent les responsabilités propres à ce backend.

> Ce logiciel est un socle de développement et de démonstration. Il n'est pas certifié pour la conduite des secours, l'évacuation, la prévision de propagation ni la confirmation automatique d'un feu.

## Position dans l'architecture événementielle v2

Le flux cible est :

```text
viewpoint privé + temps + message et/ou médias
→ EventCandidate privé et idempotent
→ analyse asynchrone, localisation ou abstention
→ revue analyste
→ publication éditeur
→ timeline et progression publiques versionnées
```

Le backend applique les invariants suivants :

- le point de prise de vue n'est jamais assimilé au point actif et n'apparaît dans aucune réponse
  publique ;
- une sortie IA ne publie rien et ne peut pas inventer une coordonnée, un périmètre ou une
  chronologie ;
- une vue imprécise produit une zone d'incertitude, un secteur ou une abstention ;
- un analyste valide ou corrige, puis un éditeur publie par une transition séparée ;
- un hotspot isolé ne crée jamais un incident public ; une déclaration officielle peut seulement
  créer un candidat privé ;
- les observations, surfaces brûlées, prévisions et simulations conservent des rôles sémantiques
  distincts.

L'implémentation v2 est protégée par les flags `FV_EVENT_V2_ENABLED`,
`FV_SUPABASE_AUTH_ENABLED`, `FV_OFFICIAL_CONNECTORS_ENABLED`,
`FV_AGENT_EVENT_PIPELINE_ENABLED`, `FV_3D_PRIMARY_ENABLED` et
`FV_V2_PUBLICATION_ENABLED`. Leur présence et les tests locaux ne constituent pas une recette des
services Supabase, Blob, PostGIS, RunPod ou fournisseurs externes en production.

Contrats spécialisés :

- [orchestration agentique](docs/AGENT_ORCHESTRATION.md) ;
- [registre de preuves](docs/EVIDENCE_REGISTRY.md) ;
- [revue humaine](docs/HUMAN_REVIEW_CONTRACT.md) ;
- [gates de publication](docs/PUBLICATION_GATES.md) ;
- [préparation au déploiement](docs/DEPLOYMENT_READINESS.md).

## Ce qui est implémenté

- API FastAPI versionnée sous `/api/v1`, contrat OpenAPI exporté et erreurs au format Problem Details.
- SQLite en mode WAL, migrations Alembic reproductibles et index spatial RTree.
- `POST /incident/detect` transactionnel, idempotent et sûr sous concurrence mono-writer.
- Matching explicable `create | attach | review` combinant distance, incertitude, temps, toponymie, confiance de source et marge entre candidats.
- Zone grise conservative : une saturation de la liste de candidats force `review`, jamais une création ou un rattachement silencieux.
- Identité stable `fire_id`, épisodes immuables `episode_id` et nouvel épisode lors d'une réactivation.
- Visibilité publique contrôlée : un candidat ou une réactivation non confirmée reste `LIMITED`; localisation et asset sont masqués jusqu'à validation humaine.
- Une observation auto-rattachée mais non vérifiée ne rafraîchit pas la chronologie publique.
- Registre de sources côté serveur. Une source inconnue ne peut pas s'auto-déclarer institutionnelle.
- Les sources de confiance utilisent un secret d'ingestion dédié, transmis dans `X-Source-Token` et stocké uniquement sous forme de hash.
- Journal d'audit append-only avec snapshots avant/après, hashes, auteur, raison et `trace_id`; des triggers SQLite interdisent `UPDATE` et `DELETE`.
- Outbox transactionnelle et table de jobs avec états, tentatives et leases, prêtes pour le branchement des workers.
- Lots médias agentiques, consentements, dispatcher RunPod asynchrone et dead-letter queue dans des tables dédiées ; la table `job` terrain/publication n'est pas réutilisée.
- Machine à états contrôlée, confirmation humaine documentée et suspension/kill switch au niveau incident.
- Résolution opérateur des observations en revue : rattacher, créer une série distincte ou rejeter.
- Manifeste viewer avec ETag, cache court, asset immuable et masquage lors d'une suspension.
- Compte administrateur unique avec session `HttpOnly`, CSRF et réauthentification ; OIDC/JWT reste configurable pour une évolution multi-utilisateur.
- Logs JSON avec `trace_id`, métriques Prometheus, headers de sécurité et limite de taille des corps HTTP.
- Sauvegarde SQLite locale validée et restauration non destructive vers une nouvelle cible,
  avec intégrité, clés étrangères, migrations, audit et triggers critiques contrôlés.
- Dockerfile non-root et Compose local.
- PostgreSQL/PostGIS pour le runtime hébergé, avec contrôle strict de la révision Alembic et des
  index spatiaux dans `/readyz`.
- Point d'entrée FastAPI pour Vercel et stockage privé local/Vercel Blob.
- Session administrateur locale G1 avec cookie `HttpOnly`, CSRF en mémoire et limitation des
  tentatives de connexion.

## Démarrage local

Pré-requis : Python 3.12 ou 3.13.

```bash
cp .env.example .env
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
alembic upgrade head
uvicorn fire_viewer.main:app --reload --host 0.0.0.0 --port 8000
```

Points d'entrée :

- documentation interactive : `http://localhost:8000/docs`
- readiness : `http://localhost:8000/readyz`
- métriques : `http://localhost:8000/metrics`
- OpenAPI : `http://localhost:8000/openapi.json`

Le profil SQLite doit rester **mono-processus / mono-writer**. Ne lancez pas plusieurs workers Uvicorn sur le même fichier. Le passage à plusieurs instances exige PostgreSQL/PostGIS.

### Compte administrateur unique

Générez le hash avec `fire-viewer-hash-admin-password`, puis configurez uniquement dans
le gestionnaire de secrets de l'environnement :

```text
FV_AUTH_MODE=local_admin
FV_LOCAL_ADMIN_USERNAME=admin
FV_LOCAL_ADMIN_PASSWORD_HASH=scrypt$...
FV_PUBLIC_REPORT_HASH_SECRET=<secret aléatoire d'au moins 32 caractères>
```

Le mot de passe en clair ne doit jamais être commité. La récupération consiste à générer un
nouveau hash, remplacer `FV_LOCAL_ADMIN_PASSWORD_HASH`, révoquer les sessions existantes et
consigner l'opération. Une sauvegarde locale chiffrée ou protégée par les permissions du compte
système peut conserver le mot de passe de secours hors du dépôt.

## Démarrage Docker

```bash
docker compose up --build
```

La migration est exécutée avant le démarrage de l'API. Le volume `fire_viewer_data` porte la base SQLite persistante.

## Exemple d'ingestion non vérifiée

```bash
curl -i \
  -X POST http://localhost:8000/api/v1/incident/detect \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: src-local-20260712-00184' \
  -d '{
    "source": {"id": "local-feed", "type": "text", "trust": "unverified"},
    "observed_at": "2026-07-12T08:18:00Z",
    "received_at": "2026-07-12T08:19:04Z",
    "geometry": {
      "type": "Point",
      "coordinates": [2.0, 46.0],
      "horizontal_uncertainty_m": 620
    },
    "evidence": {
      "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "license": "source-specific"
    },
    "context": {
      "territory_code": "83",
      "toponyms": ["Zone de démonstration fictive"],
      "canonical_name": "Zone de démonstration fictive - secteur Alpha"
    }
  }'
```

Une première observation retourne typiquement `201 create`. Une observation fortement compatible retourne `200 attach`. Deux candidats de scores proches, ou une recherche tronquée par le budget de candidats, retournent `200 review`.

## Raccordement d'une source de confiance

Le token ci-dessous doit être aléatoire, long, stocké dans un secret manager et utilisé uniquement côté serveur. Il ne doit jamais être intégré au shell web ou au build Unity.

```bash
curl -X PUT http://localhost:8000/api/v1/operator/sources/official-feed-83 \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "institutional",
    "trust": "institutional",
    "display_name": "Flux officiel 83",
    "enabled": true,
    "ingest_token": "replace-with-at-least-32-random-characters",
    "reason": "Source approuvée pour le connecteur institutionnel."
  }'
```

Le connecteur envoie ensuite le même secret dans `X-Source-Token`. Le secret n'est jamais renvoyé par l'API et n'est pas placé dans l'audit.

## Endpoints principaux

### API v2 additive

| Méthode | Route canonique | Rôle |
|---|---|---|
| `POST` | `/api/v2/evidence/uploads` | Ouvrir un upload privé authentifié |
| `POST` | `/api/v2/evidence/uploads/{upload_id}/finalize` | Finaliser et contrôler les preuves |
| `POST` | `/api/v2/event-candidates` | Créer une contribution événementielle idempotente |
| `GET` | `/api/v2/me/event-candidates` | Suivre ses contributions sans exposer celles d'autrui |
| `GET` | `/api/v2/internal/event-candidates/{candidate_id}` | Charger le dossier privé de revue |
| `POST` | `/api/v2/internal/fire-activity-events/{event_id}/validate` | Validation analyste auditée |
| `POST` | `/api/v2/internal/fire-activity-events/{event_id}/publish` | Publication éditeur avec session récente |
| `GET` | `/api/v2/incidents/{incident_id}/timeline` | Projection des seuls événements publiés |

### API v1 conservée pendant la migration

| Méthode | Route canonique | Rôle |
|---|---|---|
| `POST` | `/api/v1/incident/detect` | Ingestion idempotente et matching |
| `GET` | `/api/v1/incident/{fire_id}` | Métadonnées publiques et épisodes |
| `GET` | `/api/v1/incident/{fire_id}/manifest` | Contrat viewer, ETag, asset courant |
| `PUT` | `/api/v1/operator/sources/{source_id}` | Registre et credential des sources |
| `POST` | `/api/v1/operator/observations/{observation_id}/resolve` | Résolution d'une revue |
| `POST` | `/api/v1/operator/incidents/{fire_id}/transitions` | Transition d'état auditée |

Les anciennes routes au pluriel `/api/v1/incidents/...` restent disponibles comme alias de compatibilité, mais ne figurent pas dans OpenAPI.

En mode `local_admin`, les mutations Admin exigent la session, le jeton CSRF et, pour les actions
irréversibles, une réauthentification. En mode OIDC/JWT, les mutations opérateur exigent un bearer
JWT et utilisent le claim configuré par `FV_OIDC_ROLES_CLAIM`. La présence de ce mode dans le code
ne prouve pas qu'un IdP ou une MFA sont raccordés au déploiement actuel.

## Garanties du premier incrément

### Idempotence et concurrence

L'API prend le verrou d'écriture SQLite avec `BEGIN IMMEDIATE` avant l'allocation d'identité et le matching. La réponse complète est conservée pendant la durée de rétention. Une même clé avec un corps différent retourne `409`; après expiration, la clé peut être réutilisée sans collision avec l'outbox. Deux appels concurrents avec la même clé produisent un seul agrégat, un seul événement outbox et un rejeu explicite.

### Matching conservateur

Le RTree ne sert que de préfiltre. Le classement final utilise une distance géodésique, l'incertitude combinée, la compatibilité temporelle, la toponymie et la confiance enregistrée côté serveur. Les scores égaux sont départagés de façon déterministe par les identifiants stables. Les seuils portent un `policy_id`; ils doivent être recalibrés sur un corpus annoté avant tout usage opérationnel.

### État public

Une détection ne peut pas passer seule à `ACTIVE_CONFIRMED`. Les nouvelles séries et réactivations restent `LIMITED`; la vue publique masque leur position et leur asset. La transition vers `ACTIVE_CONFIRMED` exige un rôle `validator` et un `validation_basis` documenté.

### Audit

Les mutations importantes conservent des snapshots structurés avant/après, leurs hashes, l'acteur, la raison et le `trace_id`. Les snapshots sont minimisés et n'exposent pas les preuves brutes. Le journal est protégé par des triggers append-only contrôlés à la restauration; les tests prouvent le rejet de `UPDATE` et `DELETE`, ainsi que la vérification des hashes. Création, attachement revu, rejet et réactivation produisent des événements dédiés uniquement pour les agrégats réellement mutés.

### Authentification des sources

La confiance déclarée dans le JSON n'est jamais suffisante. Pour une source enregistrée comme partenaire, institutionnelle ou opérateur, le service vérifie `X-Source-Token` avant même de servir un rejeu idempotent.

## Qualité

```bash
make quality
```

Le rapport historique [`QUALITY_REPORT.md`](QUALITY_REPORT.md) décrit la baseline G1 au moment de
sa rédaction ; il ne doit pas être interprété comme la preuve de la révision courante. Les gates de
préparation sont suivis dans [`docs/DEPLOYMENT_READINESS.md`](docs/DEPLOYMENT_READINESS.md) ; seul
un rapport daté peut attester les commandes réellement exécutées pour une révision donnée.

Le contrat historique du manifeste viewer est documenté dans
[`docs/INTEGRATION.md`](docs/INTEGRATION.md#manifeste-viewer).
Son schéma versionné est généré depuis `ViewerManifest` avec :

```bash
make viewer-contract-schema
```

## Revue spatiale agentique

La revue Admin ne génère pas un nouveau modèle 3D. Elle charge le `ModelAsset` GLB courant comme
socle immuable et superpose des calques privés : marqueurs WGS84, révisions `MultiPolygon` de la zone
active et résultats agentiques. Les contributions utilisateur conservent un cycle distinct :
consentement, stockage privé, modération, retrait et éventuelle publication du média. Le pipeline IA
peut analyser un média privé éligible, mais sa sortie ne remplace ni la contribution ni sa décision
de publication.

Une fois toutes les opérations prévues pour la fenêtre d'analyse arrivées à une issue terminale
(succès, échec partiel, échec, annulation ou absence déclarée), leurs sorties sont consolidées dans
la revue spatiale existante. Cette barrière est contractuelle et ne dépend jamais du nombre de
fichiers, de faits ou de géométries produits. Une fenêtre pauvre ou vide doit donc présenter son
abstention ou ses limites à l'opérateur, sans donnée inventée.

La revue post-inférence expose les faits avec leur preuve conservée, le rapport privé, les
contradictions, les points et les propositions de périmètre. Dessiner, reprendre ou fusionner crée
uniquement une nouvelle révision du calque de zone. Aucun de ces actes ne crée de `ModelAsset`, ne
remplace le GLB courant ou ne publie automatiquement un contenu ; les validations du rapport, du
calque et des médias restent séparées.

Le dossier d'une contribution peut afficher en lecture seule les résultats IA qui utilisent cette
preuve. Il ne peut ni valider ni rejeter ces résultats : la revue spatiale de l'incident est l'unique
lieu de décision pour les faits, géométries et rapports produits par l'IA. La modération de la
contribution et la proposition éventuelle d'un média à la galerie restent des décisions séparées.

## Sauvegarde SQLite

```bash
fire-viewer-backup --output backups/manual.db
# ou
python -m fire_viewer.scripts.backup_sqlite --output backups/manual.db
```

La source est lue sans checkpoint forcé, puis le backup est validé (intégrité, clés étrangères,
révision, audit et triggers) avant publication. Pour une reprise, la cible doit être nouvelle :

```bash
fire-viewer-restore --source backups/manual.db --target data/fire_viewer_recovered.db
```

La restauration ne remplace jamais une cible existante et ne migre que son fichier `.part`
privé avant validation finale. La procédure et les limites sont détaillées dans
[`docs/RUNBOOK_BACKUP_RESTORE.md`](docs/RUNBOOK_BACKUP_RESTORE.md).

## Données de démonstration

Après migration :

```bash
fire-viewer-seed
```

Le seed crée le seul dataset versionné `FR-83-00042`, entièrement fictif, avec les
épisodes fixes `E01`, `E02` et `E03`. `E03` est l'épisode courant en `MONITORING`.
Il ne publie ni GLB, ni `ModelAsset`, ni révision de manifeste : la réponse viewer
attendue est donc honnêtement `not_available`.

Le script calcule et affiche le hash du manifeste courant (la même valeur que l'`ETag`
fort de l'endpoint). Un deuxième lancement vérifie que les données existantes correspondent
au dataset déclaré et ne les modifie pas. S'il rencontre un `FR-83-00042` différent, il
échoue sans l'écraser : utilisez une base de démonstration vierge plutôt qu'un reset
automatique.

Les métadonnées `.invalid` utilisées dans les tests de contrat ne correspondent à aucun
fichier ni téléchargement. Un asset GLB de démonstration vérifiable est reporté à FV-008.

## Limites assumées

- La géométrie d'ingestion est limitée à `Point` + incertitude horizontale.
- Les fonctions de score sont des paramètres de prototype G1, pas des seuils opérationnels validés.
- La préparation LiDAR reste volontairement locale ; le backend reçoit des packages déjà produits.
- Le téléversement direct/multipart des gros packages est implémenté ; la recette réelle du package
  de 417 Mo sur le store Blob de production reste à exécuter.
- L'admission v2, les uploads privés, les consentements, la revue et les transitions sont présents ;
  la recette live de l'antivirus, du Blob, de Supabase Auth et des limites d'infrastructure reste à
  exécuter avant activation publique.
- Le dispatcher RunPod est intégré au projet Vercel API sous forme de Crons bornés : preuves utilisateur toutes les 3 h, recherche médiatique à 11 h/23 h Europe/Paris, satellite/points chauds/thermique avec le run de 11 h, et suivi du job actif. Une file globale traite strictement un incendie après l'autre. Son exécution contre l'endpoint de production reste à recetter après configuration des secrets serveur.
- Les tables historiques `job` et `outbox_event` restent réservées au terrain, aux assets et à l'outbox. Leur runner n'est pas fourni par le dispatcher agentique.
- Le schéma PostgreSQL/PostGIS est migré par Alembic ; l'import automatisé depuis une base SQLite
  existante reste une phase dédiée.
- Le déploiement mono-administrateur n'a pas de MFA ; un passage multi-utilisateur exige un IdP OIDC,
  des identités nominatives, des rôles et une MFA réellement raccordés et testés.
- Le rate limiting, le WAF et la protection de `/metrics` sont à appliquer au proxy/ingress.

## Arborescence

```text
src/fire_viewer/
  api/          routes, middlewares et erreurs Problem Details
  core/         configuration, sécurité, logs, identifiants
  db/           modèles SQLAlchemy, moteur SQLite WAL, transactions
  domain/       schémas, états, géodésie et matching
  services/     ingestion, revue, transitions, manifestes
  scripts/      OpenAPI, seed, sauvegarde et vérification des migrations
migrations/     migration Alembic initiale + RTree + audit append-only
tests/          tests unitaires, intégration, concurrence et sécurité
docs/           architecture, intégration, cible PostGIS et runbook
```
