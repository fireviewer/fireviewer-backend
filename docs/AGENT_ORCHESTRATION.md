# Orchestration des analyses agentiques

**Dispatcher événementiel local :** `IMPLEMENTED_TESTED_LOCAL`

**Transport worker et collecte officielle live :** `IMPLEMENTED_NOT_LIVE_VERIFIED`

## Responsabilité du backend

Le backend est la source durable pour :

- lots ;
- fenêtres d’analyse ;
- preuves ;
- candidats de modèles ;
- consensus et cascades ;
- leases ;
- tentatives ;
- annulations ;
- dead letters ;
- revues ;
- publications.

Le worker ne décide pas seul de la suite du workflow.

## Orchestration événementielle présente

Le contrat v2 implémente localement :

- admission idempotente d’un `EventCandidate` ;
- création transactionnelle d’un unique job d’analyse privée ;
- lease de dispatch, fence de soumission, polling borné et états terminaux ;
- envoi d’un bundle `event-2.0` strict au worker, enrichi des `ExternalClaim` non rétractés liés à l’incident ou à la déclaration officielle du candidat privé ;
- persistance d’une localisation, d’un secteur ou d’une abstention ;
- validation stricte de la provenance renvoyée : chaque ancrage doit référencer un média privé exact du bundle et chaque localisation doit correspondre à une preuve spatiale préenregistrée dans l’outbox ;
- persistance des résultats cross-view sous l’état `SHADOW`, exclu des propositions et du rattachement différé à un incident ;
- création de `FireActivityEvent` privés en `DRAFT` ;
- transitions analyste puis éditeur ;
- exécution du dispatcher événementiel avant le chemin legacy dans les runners hébergé et CLI.

Le transport RunPod réel n’a pas été exercé. Une réponse de soumission ambiguë échoue explicitement au lieu d’être resoumise automatiquement.

Le registre fournit séparément des `IncidentSourcePlan`, watermarks, leases et backoff. L’orchestration d’adaptateurs et les collectes fournisseur live sont suivies dans `docs/EXTERNAL_SOURCE_CONNECTORS.md` et ne sont pas déduites de la présence d’un plan.

Le scheduler officiel est raccordé au cron privé `GET /api/v1/internal/external-sources/progress`, planifié toutes les cinq minutes dans la configuration Vercel, et à la CLI `python -m fire_viewer.scripts.run_external_source_scheduler`. Le bootstrap idempotent `python -m fire_viewer.scripts.bootstrap_official_sources` enregistre les métadonnées revues sans requête réseau. Ces points d’entrée sont testés localement ; aucune exécution cron hébergée ni collecte fournisseur live n’est revendiquée.

Les connecteurs suivent la cadence de leur collection, pas une fréquence globale unique. Leur indisponibilité produit un état de couverture et ne vaut pas absence d’incendie.

## États du job événementiel

```text
QUEUED → SUBMITTING → AWAITING_REMOTE
→ COMPLETED | ABSTAINED | FAILED
```

Un job et un seul est lié à chaque candidat. Le candidat conserve son propre état de revue ; job technique et décision métier ne sont pas fusionnés.

## Graphe de stages

Le backend fournit un plan déclaratif composé de stages et dépendances.

Le graphe peut libérer plusieurs branches logiques, mais le worker GPU conserve une exécution lourde séquentielle.

## États

- `pending`
- `ready`
- `running`
- `completed`
- `not_applicable`
- `abstain`
- `human_review`
- `partial`
- `failed`
- `cancelled`
- `dead_letter`

## Profils d’exécution

- `production_cascade`
- `validation_quorum`
- `shadow_sampling`

Le profil est enregistré avec le lot et ses résultats.

## Résultats partiels

Une étape réussie reste disponible si une étape ultérieure échoue. La consolidation indique les sorties présentes, absentes et non applicables.

## Barrière de revue

Une fenêtre devient présentable lorsque chaque opération attendue atteint un état terminal. Une fenêtre vide ou pauvre conserve ses limites et abstentions.

## Séparation des décisions

Le backend distingue :

- fait ;
- repère ;
- géométrie ;
- rapport ;
- média ;
- publication.

Aucune décision ne valide implicitement les autres.
