# Rapport qualité — Fire-Viewer backend 0.1.0

Date d'exécution : 12 juillet 2026.

## Environnement

- Python : 3.13.5 (le projet supporte Python 3.12 et 3.13)
- Base de test : SQLite, migrations Alembic appliquées sur des fichiers temporaires vierges
- Profil applicatif : `test`, authentification désactivée uniquement pour les tests

## Gate automatisé

Commande exécutée :

```bash
make quality
```

Résultats :

- Ruff lint : réussi, aucune erreur
- Ruff format : réussi, 52 fichiers conformes
- mypy strict : réussi, 44 fichiers source contrôlés
- pytest : 23 tests réussis
- couverture avec branches : 86,45 % (seuil bloquant : 80 %)
- Alembic : `upgrade -> autogenerate check -> downgrade` réussi sur base vierge
- compilation Python : réussie

## Smoke test fonctionnel

Un scénario complet a été exécuté sur une base temporaire :

1. `POST /api/v1/incident/detect` : `201 create`
2. rejeu avec la même clé et le même corps : `201`, `Idempotent-Replay: true`
3. lecture publique avant validation : visibilité `LIMITED`, localisation masquée
4. transition humaine vers `ACTIVE_CONFIRMED` : `200`, version d'épisode incrémentée à 2
5. manifeste viewer : `200`, statut confirmé, modèle `not_available` tant qu'aucun asset n'est publié
6. revalidation avec ETag : `304 Not Modified`

## Packaging et contrats

- Wheel Python construit avec succès : `fire_viewer_backend-0.1.0-py3-none-any.whl`
- Spécification OpenAPI régénérée : 6 routes canoniques documentées
- archive ZIP extraite dans un environnement vierge, installation `.[dev]` réussie, puis `make quality` réussi
- Dockerfile non-root et Compose fournis

## Actualisation FV-003

Exécutée sous CPython 3.13.2 le 12 juillet 2026, sur le dépôt organisé :

- Ruff lint et format : 54 fichiers conformes ;
- mypy strict : 45 fichiers source contrôlés, aucune erreur ;
- pytest : 40 tests réussis, couverture avec branches de 87,29 % ;
- migrations et compilation Python : réussies ;
- OpenAPI et schéma JSON `ViewerManifest` v2 régénérés puis vérifiés contre le modèle Pydantic ;
- contrat HTTP canonique, ETag/304, erreurs Problem Details, trois états de manifeste et préflight CORS `If-None-Match` couverts par les tests.

**NON VÉRIFIÉ** : Docker, wheel Python et intégration UI/API réelle avec `VITE_USE_MOCKS=false` ne font pas partie de cette actualisation.

## Actualisation FV-004/FV-005

Exécutée sous CPython 3.13.2 le 12 juillet 2026, après intégration des contrats spatiaux,
du seed fictif et de la matrice de visibilité :

- Ruff lint et format : 61 fichiers conformes ;
- mypy strict : 49 fichiers source contrôlés, aucune erreur ;
- pytest : 69 tests réussis, couverture avec branches de 88,70 % ;
- migrations SQLite `upgrade -> check -> downgrade` et compilation Python réussies ;
- CLI `fire-viewer-seed` validé deux fois sur une SQLite temporaire vierge : seconde
  exécution sans écriture, manifeste v2 conforme, SHA-256/ETag `6e27665245b74ad963ee6df22b1a5b45e2804c15b3714f967a35c2008a10d184`
  et réponse conditionnelle `304` vérifiés ;
- matrice canonique statut/visibilité, `503 incident_inconsistent`, masquage, archive
  `CLOSED` et tombstone `410` couverts par les tests backend.
- scan Gitleaks ciblé sur les 27 fichiers modifiés ou ajoutés : aucune fuite détectée.

**NON VÉRIFIÉ** : instance PostgreSQL réelle, Docker, GLB réel, rendu Unity/PNG et
raccordement UI/API avec `VITE_USE_MOCKS=false`.

Le 13 juillet 2026, sous CPython 3.13.2, FV-007 a renforcé la persistance et la reprise
SQLite avec les résultats suivants :

- Ruff lint et format : 65 fichiers conformes ;
- mypy strict : 52 fichiers source contrôlés, aucune erreur ;
- pytest : 87 tests réussis, couverture avec branches de 88,06 % ;
- compilation Python réussie ;
- migrations SQLite `upgrade -> upgrade idempotent -> check -> downgrade` réussies, avec
  les 26 triggers critiques et l'index RTree contrôlés ;
- `create/attach/review`, scores égaux, idempotence concurrente, conflits de rejeu,
  rollback de requête échouée et audit append-only sont couverts ;
- sauvegarde/restauration SQLite validées : source/WAL inchangés, hashes d'audit,
  déclencheurs critiques, corruption, cible existante, nettoyage `.part` et migration
  privée `c6d4f13a9b20 -> e7a4c9d8f2b1` ;
- la résolution des migrations est couverte dans un layout Python non éditable compatible
  avec le répertoire de travail Docker, sans construire l'image.

Aucun service hébergé, coût récurrent ou dépendance propriétaire n'est introduit par cette
passe. Il reste à tester : construction/exécution réelle de Docker, PostgreSQL/PostGIS,
stockage distant, GLB réel et Unity/WebGL.

## Limite de validation de cette livraison

Le moteur Docker n'était pas disponible dans l'environnement d'exécution ; l'image n'a donc pas été construite ici. Le Dockerfile reste couvert par la même commande de démarrage et les mêmes migrations que le profil local, mais sa construction doit être ajoutée au pipeline CI du dépôt cible.

Ce rapport documente un socle de prototype G0/G1 en cours de construction. Le gate G1 n'est pas déclaré atteint avant les preuves FV-006 à FV-010 ; il ne constitue pas une certification pour un usage opérationnel de sécurité civile.
