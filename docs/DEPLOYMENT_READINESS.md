# État de préparation du backend

Ce document décrit les gates de préparation. Les résultats de validation sont consignés dans des rapports datés.

## Domaines

### Contrats

- OpenAPI ;
- viewer ;
- public view ;
- Admin review ;
- agent batch/result ;
- evidence registry ;
- spatial proposal.

### Données

- migrations ;
- contraintes ;
- index ;
- audit ;
- sauvegarde ;
- restauration ;
- rétention ;
- purge.

### Orchestration

- leases ;
- reprise ;
- annulation ;
- résultats partiels ;
- dead letters ;
- kill switch ;
- barrière de revue.

### Sécurité

- session ;
- CSRF ;
- rôles ;
- secrets ;
- limites ;
- stockage privé ;
- headers ;
- journaux.

### Publication

- gates séparées ;
- retrait ;
- rollback ;
- suspension ;
- projection publique.

## Statuts

- `ready_for_local_validation`
- `ready_for_staging`
- `integrated_unbenchmarked`
- `blocked`
- `production_review_required`

Aucun statut n’est déduit du seul passage des tests unitaires.
