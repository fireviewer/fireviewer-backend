# Architecture du premier incrément

## Périmètre

Ce socle met en œuvre la continuité d'identité et la persistance spatiale : une série `incident_series` possède un `fire_id` stable; chaque activation ou réactivation possède un `episode_id`; les observations, décisions et mutations restent traçables.

## Transaction d'ingestion

1. Validation stricte du JSON, des temps, coordonnées, hashes et headers.
2. `BEGIN IMMEDIATE` sur SQLite afin d'obtenir le writer avant l'allocation d'identité.
3. Résolution de la source dans le registre serveur et authentification de son credential lorsqu'elle est enregistrée comme source de confiance.
4. Lecture de `(endpoint, Idempotency-Key)` et détection d'un éventuel rejeu. L'authentification est donc revérifiée même lors d'un replay.
5. Recherche conservative des candidats par intersection RTree.
6. Calcul des facteurs et de la marge entre le meilleur et le deuxième candidat.
7. Décision :
   - `create` : nouvelle série `LIMITED` + épisode E01;
   - `attach` : observation rattachée, ou nouvel épisode `UNDER_REVIEW` si l'ancien est clos;
   - `review` : proposition enregistrée sans rattachement silencieux.
8. Écriture de l'observation, de l'audit, d'un événement outbox et de la réponse idempotente.
9. Commit unique.

Une observation non vérifiée peut être associée comme preuve, mais elle ne rafraîchit pas la chronologie publique. Aucun agent externe ne modifie directement le statut public.

## Tables

- `incident_series` : identité stable, géométrie de référence, visibilité publique.
- `episode` : statut courant, chronologie et version optimiste.
- `observation` : source, temps, point, incertitude, preuve hashée, décision et facteurs.
- `source` : type, niveau de confiance et hash du credential d'ingestion.
- `model_asset` / `manifest_revision` : fondation du versioning immuable du viewer.
- `job` : état, tentatives, lease et entrées immuables des futurs workers.
- `audit_event` : journal append-only avec snapshots avant/après et hashes.
- `outbox_event` : événements à dispatcher après commit.
- `idempotency_record` : corps normalisé, réponse et expiration de rétention.
- `fire_id_counter` : allocation transactionnelle par territoire.

## Cohérence spatiale

Les coordonnées d'échange sont longitude/latitude WGS84. L'index RTree stocke des boîtes calculées à partir du point de référence et de son incertitude. Il ne décide jamais du rattachement : le classement utilise ensuite Haversine et l'incertitude combinée.

Si le nombre de candidats dépasse le budget configuré, le matcher renvoie `review`, même lorsque les candidats visibles ont un score faible. Une troncature de recherche ne peut donc pas provoquer une création automatique.

## Visibilité et états

La machine à états refuse notamment `CANDIDATE -> EXTINGUISHED`. `ACTIVE_CONFIRMED` exige un rôle `validator` et un `validation_basis`. La suspension exige `security_operator` et masque les données publiques sans supprimer l'historique.

Les séries `CANDIDATE`, `UNDER_REVIEW` ou `REJECTED` restent `LIMITED`. La localisation et l'asset du manifeste sont alors masqués. Une confirmation humaine rend la série `PUBLIC`.

## Authentification des sources

Une source non vérifiée peut être découverte automatiquement. Une source `partner`, `institutional` ou `operator` doit être provisionnée par un administrateur avec un secret d'au moins 32 caractères. Seul son hash SHA-256 est stocké. Le connecteur transmet le secret dans `X-Source-Token` sur une connexion HTTPS serveur-à-serveur.

Le token ne doit jamais être placé dans le frontend, Unity, un manifeste, un log ou un événement d'audit.

## Extension par workers

Le dispatcher futur lira `outbox_event` et publiera, au minimum :

- `observation.processed`;
- `observation.review_resolved`;
- `incident.status_changed`.

Le worker terrain utilisera `job` pour les leases, reprises et quarantaines. Le dispatcher devra marquer `published_at` après confirmation du broker et rendre les handlers idempotents au moyen de `event_id`, `trace_id` et des versions d'entrée. La mutation métier reste dans l'API transactionnelle.
