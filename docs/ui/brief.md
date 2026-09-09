# Cahier des charges de l'interface — screener PEA

À coller dans Claude Design. Tout ce qui suit décrit une interface **réelle**, alimentée par un
fichier de données que le système produit déjà. Le rendu doit fonctionner avec ce fichier, pas
avec des données inventées.

---

## 1. Ce que fait le système

Un screener d'actions européennes éligibles au PEA, auto-hébergé, qui tourne seul chaque soir sur
un serveur domestique. Il note environ mille quatre cents valeurs cotées à Paris, Amsterdam,
Bruxelles, Lisbonne et Francfort, rédige des dossiers d'investissement argumentés sur les
meilleures, et tient deux portefeuilles fictifs pour mesurer honnêtement sa performance.

**Il ne passe aucun ordre.** L'utilisateur, un développeur qui investit sur son PEA à horizon de
six mois à trois ans, lit, juge, et décide lui-même. Un seul lecteur, pas de comptes, pas de
connexion.

Le ton doit être celui d'un instrument de mesure, pas d'une application de courtage. Pas de
flèches vertes clignotantes, pas de « BUY », pas de gamification. Un document qu'on consulte le
dimanche soir avec un café, et qui ne cherche pas à faire agir.

---

## 2. Contraintes techniques, non négociables

- **HTML et CSS statiques, un seul fichier par page**, servis par un serveur de fichiers rudimentaire
  (`python -m http.server`). Aucun serveur applicatif, aucune base de données interrogeable depuis
  le navigateur, aucune étape de compilation.
- **JavaScript autorisé mais sans dépendance externe** : pas de React, pas de Vue, pas de CDN. Le
  site doit fonctionner sans accès à Internet, sur un réseau local. Tri, filtre et recherche se
  font en JavaScript natif sur les données déjà chargées.
- **Les données viennent d'un unique fichier `data.json`** placé à côté des pages, chargé par
  `fetch('data.json')`. Le schéma est donné en section 5 et ne doit pas être modifié : le système
  le produit tel quel. Prévois l'état de chargement et l'état d'erreur si le fichier manque.
- **Police système** (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`). Aucune
  police téléchargée.
- **Thème clair et sombre**, suivant le réglage du navigateur via `prefers-color-scheme`, avec un
  bouton pour forcer l'un ou l'autre, mémorisé dans `localStorage`.
- **Lisible sur téléphone** : l'utilisateur consultera souvent le digest du dimanche soir depuis son
  téléphone. Les tableaux larges défilent horizontalement dans leur propre conteneur, jamais la page.
- **Tout en français.** Les identifiants techniques du JSON restent tels quels, mais rien de ce que
  lit l'utilisateur ne doit être en anglais.
- **Chiffres alignés** : `font-variant-numeric: tabular-nums` sur toute colonne numérique.

---

## 3. Les pages

### 3.1 Accueil — le classement

La page d'entrée. Elle répond à « qu'est-ce qui mérite mon attention cette semaine ».

**En-tête** : date du classement, régime de calcul, nombre de valeurs examinées, classées, écartées,
couverture moyenne des données, empreinte de version du code. Si le régime vaut `reconstructed`,
un bandeau d'avertissement explique que la date précède le démarrage du système, que les comptes
ont pu être retraités depuis et que l'univers ne contient que des sociétés encore cotées : ce
classement montre ce que le système **aurait pu** voir, pas ce qu'il aurait vu.

**Tableau principal**, une ligne par valeur classée, colonnes :

rang, nom, secteur, pays, score total, les cinq scores de blocs, puis les métriques brutes les plus
parlantes (croissance du chiffre d'affaires sur trois ans, marge opérationnelle, rentabilité des
capitaux employés, valeur d'entreprise sur résultat opérationnel, rendement du flux de trésorerie,
dette nette sur EBITDA, momentum douze mois), la couverture des données, les signes d'alerte.

Comportements attendus :
- clic sur un en-tête pour trier, indicateur visuel du tri courant ;
- filtre par secteur et par pays, en cases à cocher ou en menu ;
- champ de recherche par nom ou code ISIN ;
- clic sur une ligne pour ouvrir la fiche de la valeur ;
- **les cinq scores de blocs doivent se lire d'un coup d'œil** : une petite barre horizontale ou un
  aplat coloré derrière le chiffre, sur une échelle de zéro à cent, plutôt qu'un nombre nu ;
- un curseur ou des boutons pour n'afficher que le premier décile, les cinquante premières, ou tout.

**Point le plus important de cette page** : une donnée manquante vaut vingt sur cent dans le score
et n'est jamais inventée. Une valeur dont la couverture est faible doit se distinguer visuellement,
par exemple un liseré ou une icône discrète, pour que l'utilisateur ne confonde jamais « bien noté »
et « bien noté avec des données complètes ». Un point d'interrogation signale déjà la composante
consensus incomplète, fréquente sur les petites valeurs sans couverture analyste.

### 3.2 Fiche d'une valeur

Ouverte depuis le classement. Une seule valeur, tout ce qu'on sait d'elle.

- **Identité** : nom, code ISIN, place de cotation, secteur, industrie, pays, cours et sa date,
  capitalisation, valeur d'entreprise.
- **Le score, décomposé** : total, puis les cinq blocs, puis chaque métrique avec sa valeur brute et
  son percentile. Il faut voir d'où vient la note, y compris ce qui la plombe. Les métriques de
  valorisation sont classées à l'intérieur du secteur, l'indiquer.
- **Ce qui manque** : liste explicite des champs absents et des métriques pénalisées. C'est une
  section à part entière, pas une note de bas de page.
- **Le dossier d'investissement** quand il existe (voir 4.1) : activité en trois phrases, origine
  réelle du chiffre d'affaires, moteurs de croissance, avantage concurrentiel et ce qui pourrait le
  faire disparaître, trois risques classés par gravité, réponse à la question « la hausse récente
  est-elle justifiée par les fondamentaux ou par un récit », signes d'alerte, catalyseurs datés,
  score de conviction, horizon, fourchette de valorisation, sources avec liens et dates.
- **Les critères d'invalidation**, mis en avant : ce sont les événements observables qui rendraient
  la thèse fausse. C'est le champ le plus important du dossier, il est vérifié automatiquement à
  chaque publication de résultats. Encadré, en haut, pas noyé.
- **Historique du rang** : petit graphique en SVG pur montrant l'évolution du score et du rang sur
  les douze dernières semaines, si les données existent.

### 3.3 Valeurs écartées

Un tableau des valeurs exclues du classement avec, pour chacune, toutes les raisons. Groupé par
raison avec un compte, dépliable. C'est une page de contrôle : elle sert à repérer qu'un filtre est
mal réglé ou qu'une donnée manque en masse.

### 3.4 Qualité des données

Couverture champ par champ, les moins renseignés en tête, avec présent, absent et pourcentage.
Nombre de valeurs sans symbole résolu. Conflits d'éligibilité PEA. Comptes trop anciens. Nombre de
dates de publication estimées plutôt que réelles.

En pied de page, la preuve d'absence de fuite : dernière date d'information utilisée, dernière date
de récupération utilisée, et la mention qu'aucune donnée postérieure à la date du classement n'est
entrée dans le calcul.

### 3.5 Portefeuilles (arrive plus tard, à prévoir dans la maquette)

**Deux portefeuilles fictifs côte à côte**, avec les mêmes règles de coûts :
- l'un piloté par des règles déterministes ;
- l'autre par un agent conversationnel qui décide seul.

Pour chacun : positions détenues avec prix de revient, poids, plus ou moins-value, valeur totale,
liquidités. Journal des décisions, chacune horodatée et motivée, jamais réécrite.

**Comparaison de performance** : les deux portefeuilles contre trois références, un ETF monde
éligible au PEA, l'indice STOXX Europe 600 dividendes réinvestis, et un bras témoin qui tire au
hasard le même nombre de valeurs dans le même univers. Courbe en SVG, plus un tableau de mesures.
Le bras témoin doit être visuellement au même niveau que le reste : si le système ne bat pas le
hasard, cela doit sauter aux yeux, pas se cacher.

Les coûts réels apparaissent partout : courtage de 1,90 € par ordre et taxe française sur les
transactions de 0,4 % à l'achat pour les sociétés françaises de plus d'un milliard d'euros.

### 3.6 Digest hebdomadaire (arrive plus tard)

Une page par semaine, archivée : nouveaux candidats avec leur thèse en trois lignes, changements de
conviction, critères d'invalidation déclenchés, performance des portefeuilles. Conçue pour être lue
sur téléphone le dimanche soir.

---

## 4. Navigation et structure

Une barre supérieure discrète : Classement, Portefeuilles, Digests, Qualité des données. Un sélecteur
de date pour consulter un classement archivé. Le thème clair ou sombre. Rien d'autre.

Les fiches de valeurs peuvent être des pages générées ou un panneau latéral ouvert depuis le
classement, au choix, tant que l'adresse est partageable.

---

## 5. Le contrat de données

Le fichier `data.json`, produit par le système, à côté des pages. Les noms de champs sont fixes.
Une valeur absente vaut `null` et **ne doit jamais être remplacée par zéro ni par un tiret dans les
calculs** ; à l'affichage, un tiret est attendu.

```json
{
  "genere_le": "2026-09-09T20:15:00Z",
  "as_of": "2026-09-09",
  "regime": "live",
  "survivorship_complete": true,
  "version_code": "01770fd",
  "derniere_information_utilisee": "2026-09-09",
  "derniere_recuperation_utilisee": "2026-09-09T19:42:11Z",
  "univers": { "examinees": 1401, "classees": 712, "ecartees": 689 },
  "couverture_moyenne": 0.83,
  "valeurs": [
    {
      "rang": 1,
      "decile": 1,
      "isin": "NL0000852564",
      "ticker": "AALB.AS",
      "nom": "AALBERTS NV",
      "secteur": "Industrials",
      "industrie": "Specialty Industrial Machinery",
      "pays": "Netherlands",
      "place": "XAMS",
      "score": 78.9,
      "blocs": {
        "croissance_qualite": 85.0,
        "momentum": 69.0,
        "valorisation": 91.0,
        "bilan": 67.0,
        "consensus": 72.0
      },
      "consensus_incomplet": false,
      "metriques": {
        "croissance_ca_3a":        { "valeur": -0.015, "percentile": 22.0, "unite": "pourcent" },
        "croissance_resultat_3a":  { "valeur": null,   "percentile": 20.0, "unite": "pourcent" },
        "marge_operationnelle":    { "valeur": 0.097,  "percentile": 48.0, "unite": "pourcent" },
        "tendance_marge_3a":       { "valeur": 0.012,  "percentile": 61.0, "unite": "points" },
        "rentabilite_capitaux":    { "valeur": 0.087,  "percentile": 55.0, "unite": "pourcent" },
        "conversion_tresorerie":   { "valeur": 1.013,  "percentile": 71.0, "unite": "ratio" },
        "momentum_12_1":           { "valeur": 0.596,  "percentile": 88.0, "unite": "pourcent" },
        "momentum_6_1":            { "valeur": 0.231,  "percentile": 79.0, "unite": "pourcent" },
        "ecart_moyenne_200j":      { "valeur": 0.142,  "percentile": 74.0, "unite": "pourcent" },
        "ve_sur_resultat_op":      { "valeur": 18.685, "percentile": 41.0, "unite": "ratio",
                                     "classe_dans_le_secteur": true },
        "rendement_flux_libre":    { "valeur": 0.033,  "percentile": 52.0, "unite": "pourcent",
                                     "classe_dans_le_secteur": true },
        "cours_sur_benefice":      { "valeur": 30.946, "percentile": 33.0, "unite": "ratio",
                                     "classe_dans_le_secteur": true },
        "dette_nette_sur_ebitda":  { "valeur": 2.019,  "percentile": 44.0, "unite": "ratio" },
        "couverture_interets":     { "valeur": 9.8,    "percentile": 63.0, "unite": "ratio" },
        "dilution_3a":             { "valeur": -0.023, "percentile": 81.0, "unite": "pourcent" },
        "revision_consensus_3m":   { "valeur": 0.039,  "percentile": 76.0, "unite": "pourcent" },
        "revisions_nettes":        { "valeur": 0.25,   "percentile": 68.0, "unite": "ratio" }
      },
      "cours": 42.68,
      "date_cours": "2026-09-09",
      "capitalisation_eur": 4610000000,
      "valeur_entreprise_eur": 5590000000,
      "capitaux_echanges_3m_eur": 8420000,
      "exercice_reference": "2025-12-31",
      "anciennete_comptes_jours": 252,
      "devise_comptes": "EUR",
      "couverture": 1.0,
      "champs_manquants": [],
      "metriques_penalisees": [],
      "drapeaux": [],
      "dossier": null
    }
  ],
  "ecartees": [
    {
      "isin": "FR0000064602",
      "nom": "ACANTHE DEV.",
      "secteur": "Real Estate",
      "raisons": ["secteur_exclu", "illiquide"]
    }
  ],
  "couverture_par_champ": [
    { "champ": "income.FY0.TotalRevenue", "present": 1204, "absent": 197 }
  ],
  "raisons_exclusion": [
    { "raison": "illiquide", "valeurs": 358 }
  ]
}
```

Quand les dossiers d'investissement existeront, le champ `dossier` d'une valeur contiendra :

```json
{
  "conviction": 74,
  "horizon_mois": 18,
  "activite": "Trois phrases décrivant l'activité.",
  "origine_du_chiffre_affaires": "D'où vient réellement le chiffre d'affaires.",
  "moteurs_de_croissance": ["...", "..."],
  "avantage_concurrentiel": "...",
  "ce_qui_le_ferait_disparaitre": "...",
  "risques": [{ "gravite": 1, "titre": "...", "explication": "..." }],
  "hausse_justifiee": { "verdict": "fondamentaux | recit | mixte", "explication": "..." },
  "signes_alerte": ["dilution", "changement de directeur financier"],
  "catalyseurs": [{ "date": "2026-11-04", "evenement": "..." }],
  "fourchette_valorisation": { "basse": 38.0, "haute": 52.0, "devise": "EUR" },
  "criteres_invalidation": [
    { "critere": "La marge opérationnelle passe sous 8 % deux semestres de suite",
      "statut": "non_declenche" }
  ],
  "sources": [{ "titre": "Rapport annuel 2025", "url": "https://...", "date": "2026-03-12" }],
  "cout_eur": 0.043,
  "version_prompt": "recherche-v3"
}
```

Le tableau `portefeuilles` viendra plus tard avec la même logique : deux entrées, `regles` et
`agent`, chacune avec ses positions, son journal et sa performance.

**Un fichier d'exemple réel accompagne ce document** : `exemple-donnees.json`. Il contient un
classement complet produit par le système, avec de vraies sociétés, leurs vraies notes et leurs
vrais trous de données. Construis l'interface sur ce fichier, pas sur des données inventées, et
vérifie que tout s'affiche correctement, y compris les valeurs dont plusieurs métriques sont
absentes.

---

## 6. Ce qu'il ne faut surtout pas faire

- **Ne jamais inventer de chiffre pour combler un trou.** Un champ absent s'affiche en tiret, et le
  fait qu'il manque doit être visible. C'est la règle la plus importante de tout le projet.
- **Ne pas afficher de recommandation d'achat ou de vente**, pas de « BUY », pas de note en étoiles,
  pas de bouton d'action. Le système propose des dossiers, l'utilisateur décide.
- **Ne pas cacher le bras témoin aléatoire** ni les contre-performances. Si le système fait moins
  bien que le hasard, la page doit le montrer aussi clairement que le contraire.
- **Ne pas ajouter de dépendance externe**, de police téléchargée, de bibliothèque de graphiques ni
  d'appel réseau. Le site doit s'ouvrir sur un réseau local sans Internet.
- **Ne pas mettre de données factices dans le rendu final.** Utilise le fichier d'exemple fourni.
- **Pas d'animation gratuite**, pas de compteur qui s'incrémente, pas de dégradé décoratif. Les
  couleurs servent à distinguer, à alerter et à ordonner, jamais à décorer.
- **Pas de mention rassurante** du type « performance garantie » ou « valeur sûre ». Un avertissement
  sobre en pied de page suffit : ce document ne constitue pas un conseil en investissement.

---

## 7. Repères de style

L'inspiration à viser est celle d'un rapport d'analyse sérieux ou d'un tableau de bord scientifique :
beaucoup de données, très peu d'ornement, une hiérarchie typographique nette, des espaces qui
laissent respirer les tableaux. Pense à un article du *Financial Times* plutôt qu'à une application
de courtage grand public.

Palette sobre, un fond légèrement teinté plutôt qu'un blanc pur, une seule couleur d'accent, du
rouge et du vert réservés aux variations et choisis pour rester lisibles par un daltonien, avec une
information redondante par la forme ou le signe.
