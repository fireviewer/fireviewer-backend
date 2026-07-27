Runbook sauvegarde et restauration SQLite — FV-007

Cette procédure reste locale et sans coût récurrent : SQLite, Python, Alembic et des
fichiers de sauvegarde locaux. Elle n'utilise aucun service cloud ni licence supplémentaire.

La sauvegarde ouvre la source en lecture seule, utilise `sqlite3.backup()` et ne force pas
de `wal_checkpoint(FULL)`. Avant publication, elle contrôle l'intégrité SQLite, les clés
étrangères, la révision Alembic, les hashes d'audit et les 26 triggers critiques. Les tests
ont aussi vérifié que le fichier source et son WAL ne changent pas pendant l'opération.

Pour créer une sauvegarde depuis le dossier backend :

```powershell
fire-viewer-backup --output backups/fire_viewer_20260713.db
Get-FileHash backups/fire_viewer_20260713.db -Algorithm SHA256
```

Conserver la sortie JSON : elle indique la révision et les comptes d'audit sans exposer le
contenu de la base. Le script travaille d'abord dans un fichier `.part`, qui disparaît en
cas d'échec. Ne pas utiliser le nom de votre seule copie comme sortie de backup, car un
backup validé peut remplacer un ancien fichier de même nom.

Pour restaurer, arrêter les écritures, garder l'ancienne base intacte et choisir une cible
qui n'existe pas :

```powershell
fire-viewer-restore `
  --source backups/fire_viewer_20260713.db `
  --target data/fire_viewer_recovered.db
```

La commande refuse une cible existante ou identique à la source. Elle valide la source en
lecture seule, copie vers `data/.fire_viewer_recovered.db.<id>.part`, applique les
migrations uniquement à cette copie, la revalide, puis publie atomiquement la nouvelle
cible. La source n'est ni migrée ni modifiée. Une sauvegarde compatible plus ancienne,
comme `c6d4f13a9b20`, est acceptée uniquement pour cette migration privée.

Après succès, démarrer une nouvelle instance avec `FV_DATABASE_URL` pointant vers la cible,
puis vérifier `/readyz` et un échantillon non sensible : incident, épisode courant,
manifeste, outbox et nombre d'audits. Réactiver les écritures seulement après ce contrôle.

Le script s'arrête sans publier de cible si l'intégrité, les clés étrangères, la révision,
les hashes, un trigger critique ou la migration sont invalides. Les tests couvrent un backup
tronqué, un hash altéré, un trigger audit affaibli, un garde-fou relationnel ou spatial
absent, une cible existante et le nettoyage du fichier `.part`.

La passe FV-007 a exécuté 87 tests backend avec 88,06 % de couverture. Docker réellement
exécuté et PostgreSQL/PostGIS ne font pas partie de cette preuve locale.
