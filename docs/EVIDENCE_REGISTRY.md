# Registre de preuves

**Registre événementiel et externe local :** `IMPLEMENTED_TESTED_LOCAL`

**Migration PostGIS et fournisseurs live :** `IMPLEMENTED_NOT_LIVE_VERIFIED`

## Objet

Le registre relie chaque observation, proposition et décision à ses entrées réelles.

Dans le contrat événementiel v2, il relie aussi candidats, événements, enveloppes et sources externes à leurs entrées. Les tables et services locaux existent ; la collecte fournisseur et la migration sur la base réelle ne sont pas validées.

## Entités

### Evidence source

Source originale : média, texte, capteur, produit satellite ou publication archivée.

### Evidence artifact

Artefact dérivé : frame, crop, segment audio, transcription, boîte, masque, heatmap, OCR, correspondances, pose ou raycast.

### Evidence link

Relation typée entre un artefact et son parent.

### Evidence use

Référence d’une observation ou d’une proposition vers l’artefact utilisé.

### `EvidenceAsset` et rattachement candidat

Chaque image ou vidéo finalisée est rattachée à un unique `EventCandidate` après contrôle de propriété, MIME, taille, hash et antivirus.

La finalisation et la lecture interne matérialisent l’objet privé par flux dans un fichier temporaire, recalculent sa taille et son SHA-256 par blocs, puis suppriment le fichier après usage ou après envoi de la réponse. Le chemin Blob autorisé est exact : un jeton destiné à un média ne permet pas de lire un objet voisin. Les requêtes HTTP Range restent une capacité à ajouter pour les vidéos volumineuses.

### `FireActivityEventEvidence`

Relation normalisée plusieurs-à-plusieurs entre un `EvidenceAsset` et un `FireActivityEvent`. Le stockage actuel conserve un rôle de support explicite. Les rôles de contradiction ou de contexte sont portés par la revue et les relations ; leur normalisation complète reste à étendre.

### `ExternalArtifactRevision`

Révision d’un produit externe conservant identifiant fournisseur, collection, temps, hash, licence, CRS, footprint et parents.

### `ExternalClaim`

Assertion structurée et immuable liée à une révision d’artefact et, lorsqu’il existe, à un incident. Elle conserve type, payload contrôlé, géométrie et précision éventuelles, confiance et famille indépendante. Les assertions non rétractées pertinentes sont projetées dans le bundle agent ; une déclaration officielle peut aussi ancrer la création d’un `IncidentCandidate` privé, jamais d’un incident public.

### `ArtifactLineage`

Relation append-only entre deux révisions externes : dérivation, même acquisition, remplacement, rétractation, miroir, contradiction ou dépendance restreinte.

### `ActivityEnvelopeSupport`

Relation normalisée entre une révision d’enveloppe et les événements qui la soutiennent. La table existe ; le moteur d’enveloppe reste à implémenter.

## Champs minimaux

- identifiant ;
- lot ;
- média ;
- type ;
- URI privée ;
- empreinte ;
- format ;
- dimensions ou durée ;
- parent ;
- modèle et révision ;
- contrat ;
- date de création ;
- statut de validation ;
- rétention ;
- trace d’audit.

## Règles

- un artefact ne traverse pas silencieusement les lots ;
- les `evidence_refs` doivent exister ;
- un retrait de source déclenche l’identification des dérivés ;
- une correction humaine crée une nouvelle révision ;
- une correction ne devient pas automatiquement une donnée d’entraînement ;
- les preuves brutes ne sont pas exposées dans la projection publique par défaut.
- une même URL dont le contenu change crée une nouvelle révision ;
- deux produits dérivés d’une même acquisition ne sont pas deux preuves indépendantes ;
- une déduplication de preuve ne fusionne jamais automatiquement deux événements.
- une révision externe ou sa filiation n’est ni mise à jour ni supprimée ; une correction produit une nouvelle révision ;
- une preuve rattachée durable n’est jamais supprimée par le nettoyage des uploads temporaires.
