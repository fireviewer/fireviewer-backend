# Dataset de démonstration v1

Ces artefacts décrivent le seul jeu de données public de démonstration du dépôt :
`FR-83-00042`. L'identifiant, les noms, les coordonnées et les dates sont synthétiques ;
ils ne décrivent ni un feu ni une localisation opérationnelle.

- `seed-manifest.json` est le `ViewerManifest` v2 obtenu après migration d'une SQLite
  vierge puis `fire-viewer-seed`, avec les paramètres publics par défaut.
- `seed-manifest.sha256` contient le SHA-256 du JSON canonique (clés triées, séparateurs
  compacts), qui est aussi la valeur de l'`ETag` fort sans les guillemets HTTP.
- `visibility-matrix.json` fixe les projections publiques autorisées par la machine à
  états. Les entrées `.invalid` ne représentent aucun fichier ni téléchargement.

Le seed de référence ne publie pas d'asset. Son manifeste est donc `not_available`.
Les cas `available` existent uniquement pour vérifier le contrat avec des métadonnées
fictives et restent sans GLB jusqu'à FV-008. La connexion de ce manifeste à la page web
reste FV-006.
