# Cible PostgreSQL/PostGIS

SQLite WAL convient au prototype mono-instance. Le passage à plusieurs API ou writers impose PostgreSQL/PostGIS.

## Modifications de schéma recommandées

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

ALTER TABLE incident_series
  ADD COLUMN reference_geog geography(PointZ, 4326);

ALTER TABLE observation
  ADD COLUMN geometry_geog geography(PointZ, 4326);

CREATE INDEX incident_series_reference_geog_gix
  ON incident_series USING gist (reference_geog);

CREATE INDEX observation_geometry_geog_gix
  ON observation USING gist (geometry_geog);
```

Les colonnes longitude/latitude peuvent être conservées pendant une migration en deux temps, puis alimentées par trigger ou supprimées après validation.

## Recherche de candidats

```sql
SELECT i.id
FROM incident_series AS i
JOIN episode AS e
  ON e.incident_id = i.id AND e.is_current
WHERE ST_DWithin(
  i.reference_geog,
  ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
  :search_radius_m
);
```

La distance exacte et les facteurs explicables restent dans le service de matching. `ST_DWithin` remplace uniquement le préfiltre RTree.

## Concurrence

- utiliser `SELECT ... FOR UPDATE` sur la série et l'épisode courant;
- remplacer le compteur par une ligne verrouillée, une séquence par territoire ou une fonction SQL atomique;
- conserver les contraintes partielles « un épisode courant » et « un manifeste courant »;
- exécuter les handlers d'outbox et les leases de `job` avec `FOR UPDATE SKIP LOCKED`;
- tester le niveau d'isolation et les deadlocks avec deux writers réels.

## Plan de migration

1. Export cohérent SQLite et vérification `integrity_check`.
2. Migration du schéma PostGIS sur staging.
3. Copie des lignes en conservant tous les identifiants publics.
4. Reconstruction des géographies et comparaison des distances sur un corpus gelé.
5. Vérification des comptes, jobs, hashes, révisions de manifeste et snapshots d'audit.
6. Double lecture contrôlée, puis bascule.
7. Gel et archivage de la base SQLite source.

Aucune renumérotation de `fire_id`, `episode_id`, `observation_id` ou `asset_id` n'est autorisée.
