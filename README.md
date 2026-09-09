# pea — screener PEA

Système auto-hébergé qui identifie des actions européennes éligibles au PEA à fort potentiel
sur un horizon de 6 mois à 3 ans, et qui les présente sous forme de dossiers argumentés.

**Il ne passe aucun ordre et ne se connecte à aucun courtier.** Il produit des recommandations
que tu valides toi-même, et tient un portefeuille fictif pour mesurer honnêtement sa performance.

## Livraison

| Lot | Contenu | État |
| --- | --- | --- |
| 1 | Univers, données, score composite | livré |
| 2 | Dossiers d'investissement, débat haussier contre baissier | à venir |
| 3 | Deux portefeuilles fictifs, règles et agent, journal, digest par courriel | à venir |
| 4 | Évaluation (références, bras témoin, Sharpe déflaté) | à venir |

## Commandes

```bash
make install                    # uv sync (Python 3.12)
make universe                   # construit/rafraîchit la liste des valeurs éligibles
make ingest                     # prix, FX, états financiers, consensus (long la première fois)
make screen                     # classement du jour
make screen AS_OF=2026-03-15    # même classement régénéré avec les données de cette date
make report AS_OF=2026-03-15    # re-génère les fichiers d'un run déjà calculé
make test                       # pytest (sans réseau)
```

## Le score composite

Score de 0 à 100 sur l'univers filtré. La valorisation est classée par percentile à
l'intérieur de chaque secteur.

| Bloc | Poids | Métriques |
| --- | --- | --- |
| Croissance et qualité | 35 | croissance du chiffre d'affaires et du résultat opérationnel sur 3 ans, marge opérationnelle et sa tendance, rentabilité des capitaux employés, conversion du résultat en trésorerie |
| Momentum | 25 | performance 12 mois et 6 mois hors dernier mois, position par rapport à la moyenne mobile 200 jours |
| Valorisation relative | 20 | valeur d'entreprise sur résultat opérationnel, rendement du flux de trésorerie disponible, cours sur bénéfice |
| Solidité du bilan | 10 | dette nette sur EBITDA, couverture des intérêts, dilution sur 3 ans |
| Dynamique du consensus | 10 | variation du consensus de bénéfice sur 3 mois, révisions nettes |

Filtres appliqués avant le score : liquidité médiane inférieure à 150 000 € par jour, non
éligible au PEA, dette nette sur EBITDA supérieure à 4, flux de trésorerie négatif trois
années de suite, plus de 40 % des dix-neuf champs requis manquants, sociétés financières et
foncières, classes d'actions en double.

## Décisions par défaut

Prises pour avancer sans te bloquer. Chacune est réversible ; elles sont testées.

### Univers et éligibilité

1. **Périmètre** : Euronext marchés réglementés et Growth (XPAR, ALXP, XAMS, ALXA, XBRU, ALXB,
   XLIS, ALXL) plus Xetra (XETR, lignes `Instrument Type = CS`). Euronext Access (XMLI, MLXB,
   ENXL) est exclu : ces valeurs sont presque toutes absentes de Yahoo et seraient de toute façon
   écartées par le filtre de liquidité. Seuls les ISIN de l'UE ou de l'EEE entrent dans l'univers :
   sans ce filtre, sept cents lignes américaines cotées à Xetra consommeraient des requêtes pour
   être écartées ensuite. Un ISIN du fichier d'exceptions traverse ce filtre.
   Au 9 septembre 2026 l'univers compte **1 401 valeurs** (551 Xetra, 320 Paris, 285 Growth Paris,
   104 Bruxelles, 101 Amsterdam, 32 Lisbonne, 8 Growth Bruxelles et Lisbonne).
2. **Une ligne par ISIN.** La place de référence est celle du pays de l'ISIN (FR→XPAR, NL→XAMS,
   BE→XBRU, PT→XLIS, DE→XETR), sinon la première présente. Les autres cotations restent dans
   `listings` mais ne sont pas ingérées.
3. **Éligibilité PEA** : éligible si le préfixe ISIN **et** le pays Yahoo sont dans l'UE27 + IS, LI,
   NO. Désaccord entre les deux → `pea_conflict`, pays inconnu → `pea_unknown` : la valeur est
   écartée et listée dans le rapport de couverture. Le fichier versionné
   `data/overrides/pea_eligibility.csv` tranche à la main les rares cas connus (STMicroelectronics :
   siège social aux Pays-Bas, adresse Yahoo en Suisse). Sans ce fichier le système serait faux sur
   ces valeurs ; c'est de la donnée, pas une option de configuration.
4. **Ticker Yahoo** : mnémonique plus suffixe de place, validé par les métadonnées Yahoo ; à défaut,
   résolution par ISIN ; sinon la valeur est marquée non résolue et apparaît dans le rapport.
   L'ISIN vient toujours des listes de bourse, jamais de Yahoo : le champ ISIN de Yahoo est faux
   (il donne FR0000053951 pour Air Liquide, dont l'ISIN réel est FR0000120073, et un ISIN argentin
   pour ASML).
5. **Radiations** : une valeur absente des listes reçoit une date de sortie et n'est jamais
   supprimée. Si elle réapparaît, la date est effacée. C'est ce qui protège les évaluations
   historiques du biais de survie, à partir de la première liste enregistrée.
6. **Secteurs** : classification Yahoo. Sociétés financières et foncières exclues au lot 1. Secteur
   inconnu : la valeur est conservée (pas de preuve d'exclusion) et ses percentiles de valorisation
   sont calculés sur tout l'univers.
7. **Classes d'actions doubles** (ordinaires et préférentielles allemandes) : seule la ligne la plus
   liquide est retenue.

### Données et absence de fuite

8. **Fondamentaux annuels uniquement.** Les états trimestriels sont stockés mais pas utilisés :
   ils sont vides pour les sociétés françaises à publication semestrielle, et mélanger des données
   sur douze mois glissants avec des exercices annuels fausserait les classements par secteur.
   Conséquence assumée : un fondamental peut avoir jusqu'à seize mois.
9. **Date de disponibilité d'un état** : la première des trois dates suivantes, la date de première
   récupération, la date de publication réelle si elle est connue, ou la clôture de l'exercice plus
   le délai réglementaire maximal (120 jours en annuel, 90 en semestriel, 60 en trimestriel).
10. **Deux régimes de calcul**, enregistrés avec chaque run. En régime `live` (date postérieure au
    démarrage du système) la disponibilité est pilotée par la date de récupération : le calcul à une
    date passée reproduit exactement le run de ce soir-là. En régime `reconstructed` (date
    antérieure) les valeurs peuvent être retraitées, le consensus est absent pour tout le monde et
    l'univers est biaisé par la survie : le rapport le dit en tête.
11. **Prix** : on stocke les cours bruts, les dividendes et les splits. Le cours ajusté de Yahoo est
    conservé mais jamais utilisé pour un calcul, car son ajustement rétroactif rend une série
    construite jour après jour incohérente. Le momentum passe par un indice de rendement total
    reconstruit à la demande.
12. **Devises** : les ratios sans dimension restent dans la devise des états. Seuls les multiples
    qui mêlent capitalisation en euros et compte de résultat sont convertis, au taux du jour de
    calcul. Devise ou taux inconnus : ces trois multiples sont manquants, jamais supposés.
13. **Débit Yahoo** : pause de 30 secondes toutes les 100 requêtes, temporisation croissante en cas
    de blocage, puis abandon de la valeur et passage à la suivante. Les réponses brutes sont mises
    en cache sur disque, ce qui rend toute reprise gratuite.
14. **Cadence** : ingestion et classement chaque soir de semaine à 22 h, rafraîchissement de
    l'univers le dimanche. Une valeur voit ses fondamentaux rafraîchis une fois par semaine.

### Score

15. **Une donnée manquante n'est jamais imputée.** Elle vaut 20 sur 100 dans le classement, elle
    est listée valeur par valeur et comptée dans le rapport de couverture. Un dénominateur négatif
    ou nul n'est pas une donnée manquante mais une information : il donne le pire score.
16. **On n'écarte une valeur que sur preuve positive**, sauf pour l'éligibilité PEA et la liquidité
    qui doivent être établies. Un endettement inconnu n'élimine pas ; il pénalise déjà le bloc
    bilan.
    Tous les filtres sont évalués, chaque valeur porte la liste complète de ses raisons d'exclusion.
17. **Percentiles** calculés sur les seules valeurs retenues ; la valorisation est classée dans le
    secteur, avec repli sur l'univers entier quand le secteur compte moins de dix valeurs.
18. **Les poids et seuils du score sont des constantes du code**, pas de la configuration : ils
    viennent du cahier des charges et sont versionnés par le SHA git enregistré avec chaque run.
    `config.toml` ne porte que les chemins, le débit Yahoo, les places et les coûts.

## Résultat du premier classement, 9 septembre 2026

| | |
| --- | --- |
| valeurs examinées | 1 401 |
| classées | 335 |
| écartées | 1 066 |
| couverture des champs requis | 87 % |
| durée du calcul | 9 secondes |

Les exclusions, par ordre d'importance : 816 pour liquidité insuffisante, 244 pour secteur exclu,
205 pour flux de trésorerie négatifs trois ans de suite, 180 pour endettement excessif, 167 pour
données insuffisantes, 144 pour dette sans résultat pour la rembourser, 63 pour liquidité inconnue,
6 pour conflit d'éligibilité et 3 classes d'actions en double.

Le filtre de liquidité domine parce que la valeur européenne médiane de cet univers ne traite que
35 000 € par jour, loin sous le seuil de 150 000 € que tu as fixé.

## Limites connues

- Yahoo ne conserve que quatre à cinq exercices, aucun historique de consensus et aucune valeur
  radiée. Un classement régénéré pour une date antérieure au démarrage du système porte des drapeaux
  explicites et n'équivaut pas à un vrai calcul de l'époque. Le backtest quantitatif du lot 3
  demandera un fournisseur avec fondamentaux historiques datés.
- Les dates de publication sont le plus souvent estimées avec le délai réglementaire maximal, donc
  prudentes. Le taux d'estimation figure dans le rapport de couverture.
- Les volumes Yahoo ne couvrent que la place principale : la liquidité mesurée est conservatrice.
- La qualité des données Yahoo est inégale sur les petites valeurs. Le rapport de couverture est
  l'instrument de surveillance.
- La liste Euronext ne distingue pas les actions des bons de souscription, contrairement à celle de
  Xetra. Une quarantaine de ces lignes entrent donc dans l'univers, échouent à la résolution ou à la
  collecte, puis sont listées comme non résolues et mises de côté. Elles coûtent quelques requêtes
  une seule fois.

## Déploiement

Un seul conteneur, qui sert les rapports sur le port 8080 et exécute les tâches planifiées
(`deploy/crontab`) : ingestion et classement du lundi au vendredi à 22 h, rafraîchissement
de l'univers le dimanche à 10 h.

```bash
docker compose up -d --build
```

Le conteneur tourne sans les droits d'administrateur, sous l'utilisateur d'identifiant 10001. Le
répertoire `data` monté depuis l'hôte doit donc lui appartenir, sans quoi le service ne peut rien
écrire :

```bash
sudo chown -R 10001:10001 data
```

Les données vivent dans `./data`, monté dans le conteneur : la base DuckDB, le cache des
réponses brutes et les rapports. La première collecte se lance à la main, une seule fois, et se
détache pour survivre à la fermeture du terminal :

```bash
docker exec -d pea sh -c "pea universe && pea ingest && pea screen"
```

Elle prend deux à trois heures ; les collectes suivantes une quarantaine de minutes, puisque les
fondamentaux d'une valeur ne sont rafraîchis qu'une fois par semaine. Une reprise après
interruption ne coûte aucune requête, le cache disque faisant foi pour la journée.

Ne fais pas tourner deux collectes en même temps sur deux machines derrière la même adresse
publique : Yahoo compte les requêtes par adresse et les bloquerait.

## Structure

```
pea/config.py       lecture de config.toml et de .env
pea/db.py           connexion DuckDB et schéma
pea/universe.py     listes de bourse, éligibilité, radiations
pea/data/           interface DataProvider, implémentation Yahoo, ingestion
pea/screen/         calcul à une date donnée, métriques, score, rapports
pea/cli.py          ligne de commande
scripts/            capture des réponses Yahoo servant de fixtures
deploy/             crontab et point d'entrée du conteneur
```

Les secrets vont dans `.env` (voir `.env.example`), jamais dans le dépôt.
