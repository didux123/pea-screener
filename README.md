# pea — screener PEA

Système auto-hébergé qui identifie des actions européennes éligibles au PEA à fort potentiel
sur un horizon de 6 mois à 3 ans, et qui les présente sous forme de dossiers argumentés.

**Il ne passe aucun ordre et ne se connecte à aucun courtier.** Il produit des recommandations
que tu valides toi-même, et tient un portefeuille fictif pour mesurer honnêtement sa performance.

## Livraison

| Lot | Contenu | État |
| --- | --- | --- |
| 1 | Univers, données, score composite | en cours |
| 2 | Dossiers d'investissement (LLM) | à venir |
| 3 | Portefeuille fictif, journal, rapport, Telegram | à venir |
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

## Décisions par défaut

Prises pour avancer sans te bloquer. Chacune est réversible ; elles sont testées.

### Univers et éligibilité

1. **Périmètre** : Euronext marchés réglementés et Growth (XPAR, ALXP, XAMS, ALXA, XBRU, ALXB,
   XLIS, ALXL) plus Xetra (XETR, lignes `Instrument Type = CS`). Euronext Access (XMLI, MLXB,
   ENXL) est exclu : ces valeurs sont presque toutes absentes de Yahoo et seraient de toute façon
   écartées par le filtre de liquidité.
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
12. **Devises** : les ratios sans dimension restent dans la devise des états. Seuls les multiples qui
    mêlent capitalisation en euros et compte de résultat sont convertis, au taux du jour de calcul.
    Taux ou devise de publication inconnus : ces trois multiples sont manquants, jamais supposés.
13. **Débit Yahoo** : pause de 30 secondes toutes les 100 requêtes, temporisation croissante en cas
    de blocage, puis abandon de la valeur et passage à la suivante. Les réponses brutes sont mises en
    cache sur disque, ce qui rend toute reprise gratuite.
14. **Cadence** : ingestion et classement chaque soir de semaine à 22 h, rafraîchissement de
    l'univers le dimanche. Une valeur voit ses fondamentaux rafraîchis une fois par semaine.

### Score

15. **Une donnée manquante n'est jamais imputée.** Elle vaut 20 sur 100 dans le classement, elle est
    listée valeur par valeur et comptée dans le rapport de couverture. Un dénominateur négatif ou nul
    n'est pas une donnée manquante mais une information : il donne le pire score.
16. **On n'écarte une valeur que sur preuve positive**, sauf pour l'éligibilité PEA et la liquidité
    qui doivent être établies. Un endettement inconnu n'élimine pas ; il pénalise déjà le bloc bilan.
    Tous les filtres sont évalués, chaque valeur porte la liste complète de ses raisons d'exclusion.
17. **Percentiles** calculés sur les seules valeurs retenues ; la valorisation est classée dans le
    secteur, avec repli sur l'univers entier quand le secteur compte moins de dix valeurs.
18. **Les poids et seuils du score sont des constantes du code**, pas de la configuration : ils
    viennent du cahier des charges et sont versionnés par le SHA git enregistré avec chaque run.
    `config.toml` ne porte que les chemins, le débit Yahoo, les places et les coûts.

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

## Structure

```
pea/config.py     lecture de config.toml et de .env
pea/db.py         connexion DuckDB et schéma
pea/universe.py   listes de bourse, éligibilité, radiations
pea/data/         interface DataProvider, implémentation Yahoo, ingestion
pea/screen/       accès à une date donnée, métriques, score, rapports
```

Les secrets vont dans `.env` (voir `.env.example`), jamais dans le dépôt.
