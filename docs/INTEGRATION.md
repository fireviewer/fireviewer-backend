# Guide de raccordement

## Contrat HTTP

Base par défaut : `/api/v1`.

Routes canoniques :

- `POST /incident/detect`
- `GET /incident/{fire_id}`
- `GET /incident/{fire_id}/manifest`

Les routes historiques au pluriel restent disponibles comme alias, mais le code client doit utiliser les routes canoniques au singulier.

## Headers d'ingestion

| Header | Obligatoire | Usage |
|---|---:|---|
| `Content-Type: application/json` | oui | Corps JSON strict |
| `Idempotency-Key` | oui | 8 à 128 caractères sûrs, stable pour un même événement |
| `X-Source-Token` | source de confiance | Secret d'ingestion provisionné côté serveur |
| `X-Trace-Id` | non | Corrélation fournie par le client si son format est valide |

Une même `Idempotency-Key` avec un corps différent retourne `409`. Une réponse rejouée porte `Idempotent-Replay: true`.

## Sémantique de `POST /incident/detect`

- `201 create` : une série candidate et son épisode E01 ont été créés. La série reste `LIMITED`.
- `200 attach` : l'observation est associée à une série. Elle reste à vérifier et ne modifie pas seule le statut public.
- `200 review` : le système a trouvé une ambiguïté ou une recherche tronquée; `proposed_fire_id` est une proposition, pas une mutation.

Le client doit conserver `trace_id`, `observation_id`, `policy_id`, `score`, `factors`, `margin_to_second_candidate` et `review_reasons` dans ses logs techniques.

## Erreurs

Les erreurs sont renvoyées en `application/problem+json` :

```json
{
  "type": "urn:fire-viewer:error:validation_error",
  "title": "Request validation failed",
  "status": 422,
  "detail": "One or more request fields are invalid.",
  "instance": "/api/v1/incident/detect",
  "trace_id": "tr-..."
}
```

Ne pas parser `detail` pour piloter la logique. Utiliser le suffixe stable de `type` et le code HTTP.

## Manifeste viewer

`GET /incident/{fire_id}/manifest` renvoie un ETag. Le shell doit envoyer `If-None-Match` lors de la revalidation et accepter `304`.

- Un manifeste courant utilise un cache court.
- Un asset GLB référencé doit être traité comme immuable et vérifié par `sha256` et `size_bytes`.
- `model_state=withheld` signifie que l'asset et la position sont volontairement masqués.
- `model_state=not_available` signifie qu'aucun asset publié n'est disponible.
- Le texte, le statut, la fraîcheur et l'incertitude doivent rester affichables sans Unity.

## Branchement d'un connecteur institutionnel

1. Un administrateur provisionne la source via `PUT /operator/sources/{source_id}` et transmet le secret par un canal séparé.
2. Le secret est injecté dans le service connecteur via un secret manager.
3. Le connecteur appelle `POST /incident/detect` avec `X-Source-Token`.
4. Le connecteur réutilise la même `Idempotency-Key` lors des retries réseau.
5. Il ne réessaie pas en boucle sur `4xx`; il applique backoff et jitter sur les erreurs transitoires `503`.

## Branchement du shell web

1. Charger les métadonnées et le manifeste avant Unity.
2. Rendre la vue texte immédiatement.
3. Vérifier le schéma, l'origine de l'asset, le hash et la taille.
4. Initialiser Unity uniquement si le navigateur le permet.
5. Sur erreur GLB, conserver le dernier état textuel et passer en mode dégradé.

## Environnements

- `development` : authentification opérateur désactivable.
- `test` : bases temporaires et fixtures.
- `staging` / `production` : OIDC/JWT obligatoire.

Les trusted hosts, CORS, TLS, rate limits, WAF et l'accès à `/metrics` doivent être configurés au niveau du déploiement.
