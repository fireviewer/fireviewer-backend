# Registre des sources de recherche IA

Le registre exécutable est `fire_viewer.core.research_sources`. Sa version
`firewarning-fr-sources-2026-07-19-v1` est issue du registre France vérifié le
19 juillet 2026. Une source absente de ce code est refusée, même si son domaine est
ajouté à une variable d’environnement.

Le registre contrôle l’accès réseau et l’usage sémantique d’une source. Il ne transforme
jamais une publication en vérité validée ni en contenu publiable.

## Niveaux et usages

- **A+ — opérationnel** : autorités, préfectures et services de secours habilités à
  documenter un événement, des moyens ou une consigne dans leur périmètre ;
- **A — technique** : météo, satellite et qualité de l’air, uniquement pour leur produit ;
- **B — contexte** : historique, prévention, exposition ou identité d’un domaine officiel ;
- **lead — piste privée** : presse autorisée, à recouper avant toute confirmation.

Le brouillon situationnel privé doit rechercher et conserver séparément :

- progression, surface et état communiqué du feu ;
- évacuations, confinements, hébergements et ordres à la population ;
- effectifs, moyens terrestres, avions et hélicoptères engagés ;
- victimes, dégâts, routes, accès et services interrompus ;
- alertes de pollution ou de qualité de l’air ;
- appels aux dons ou au soutien des pompiers ;
- heure, source et statut officiel ou rapporté de chaque chiffre.

Deux valeurs contradictoires ne sont jamais fusionnées. Elles restent attribuées à leur
source et à leur heure dans le rapport soumis à l’administrateur.

## Domaines opérationnels France et Die

- `securite-civile.interieur.gouv.fr`, `interieur.gouv.fr` — bilans nationaux,
  coordination, moyens et consignes ;
- `mairie-die.fr` — points d’information et consignes de la Ville de Die ;
- `drome.gouv.fr` — décisions, évacuations, restrictions et points préfectoraux ;
- `pompiers26.com` — opérations et moyens du SDIS 26 ;
- `lannuaire.service-public.fr` — validation de l’identité d’un domaine public, pas preuve
  d’un fait opérationnel.

L’ordre de priorité en cas de contradiction est : préfecture pour les décisions et accès,
SDIS pour les opérations et moyens, Sécurité civile pour la coordination nationale, puis
portails techniques et enfin presse.

## Sources techniques

- NASA : `firms.modaps.eosdis.nasa.gov`, `earthdata.nasa.gov` ;
- EFFIS : `forest-fire.emergency.copernicus.eu`,
  `maps.effis.emergency.copernicus.eu`, `data.effis.emergency.copernicus.eu` ;
- Sentinel : `dataspace.copernicus.eu`, `browser.dataspace.copernicus.eu`,
  `catalogue.dataspace.copernicus.eu`, `documentation.dataspace.copernicus.eu` ;
- Météo-France : `meteofrance.com`, `vigilance.meteofrance.fr`,
  `portail-api.meteofrance.fr`, `meteo.data.gouv.fr`,
  `donneespubliques.meteofrance.fr` ;
- qualité de l’air : `atmo-france.org`, `atmo-auvergnerhonealpes.fr` ;
- contexte : `bdiff.agriculture.gouv.fr`, `georisques.gouv.fr`, `data.gouv.fr`,
  `auvergne-rhone-alpes.developpement-durable.gouv.fr`.

Météo-France ne confirme jamais à lui seul un feu actif. FIRMS, EFFIS et Copernicus
décrivent une anomalie ou une observation technique, jamais automatiquement une
évacuation, une cause, une extinction ou un incendie confirmé. Atmo documente une mesure
ou une alerte de qualité de l’air, pas l’origine certaine du panache.

## Presse et médias

La recherche privée autorise :

- `france3-regions.francetvinfo.fr` ;
- `francebleu.fr` ;
- `ledauphine.com`.

Le texte de presse peut alimenter le brouillon comme **affirmation rapportée à recouper**,
avec titre, URL, heure et attribution. Il ne devient pas une confirmation opérationnelle.

Les images et vidéos provenant de la presse, d’une mairie, d’une préfecture ou d’un SDIS
peuvent être téléchargées et analysées dans l’espace privé. Ce droit d’analyse ne vaut
jamais droit de republication : chaque média garde son crédit, sa licence et la politique
`per_item_license_check` ou `private_analysis_only`. Un accès gratuit à une page ne vaut
pas licence. Aucun filigrane n’est retiré et aucune image n’est publiée automatiquement.

## Fournisseur et politique réseau

`html.duckduckgo.com` sert uniquement à découvrir des liens. Une page du fournisseur ne
peut ni devenir une preuve ni être persistée comme candidate. Le courtier ne retourne que
des URL appartenant aux domaines sources exacts ci-dessus.

- HTTPS et port 443 uniquement ;
- aucun domaine générique ni joker ;
- refus des adresses privées, des redirections interdites et des fichiers trop gros ;
- cache et cadence minimale définie par source ;
- arrêt ou ralentissement sur `403` et `429`, sans contournement ;
- date de publication ou d’acquisition antérieure ou égale à la coupure quotidienne.

## Métadonnées et publication

La provenance conserve au minimum domaine, URL, producteur ou attribution, type de
source, types d’affirmations autorisés, date de publication, date d’observation ou
d’acquisition, date de récupération, niveau de confiance, licence, crédit média et
empreinte du contenu. L’heure d’observation n’est jamais remplacée par l’heure de
publication.

Les candidats, faits, images, calques et rapports restent privés. La licence et le niveau
de confiance sont présentés à l’administrateur, mais seule une validation humaine peut
valider le rapport ou autoriser une publication.

Toute extension du registre exige une modification de code, un test du domaine exact et
une nouvelle version du registre. Les domaines, licences et quotas doivent être revérifiés
au moins tous les six mois.
