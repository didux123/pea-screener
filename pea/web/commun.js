/* Screener PEA — chargement des données, formatage, thème. Sans dépendance. */
(function () {
  var TIRET = '\u2014';

  var SECTEURS = {
    'Industrials': 'Industrie',
    'Technology': 'Technologie',
    'Healthcare': 'Santé',
    'Basic Materials': 'Matériaux de base',
    'Consumer Cyclical': 'Consommation cyclique',
    'Consumer Defensive': 'Consommation de base',
    'Financial Services': 'Services financiers',
    'Energy': 'Énergie',
    'Communication Services': 'Communication',
    'Real Estate': 'Immobilier',
    'Utilities': 'Services aux collectivités'
  };

  var BLOCS = {
    croissance_qualite: 'Croissance & qualité',
    momentum: 'Momentum',
    valorisation: 'Valorisation',
    bilan: 'Bilan',
    consensus: 'Consensus'
  };
  var BLOCS_COURT = {
    croissance_qualite: 'Crois.',
    momentum: 'Mom.',
    valorisation: 'Valo.',
    bilan: 'Bilan',
    consensus: 'Cons.'
  };
  var POIDS = { croissance_qualite: 0.30, momentum: 0.20, valorisation: 0.25, bilan: 0.15, consensus: 0.10 };

  var METRIQUES = {
    croissance_ca_3a: ['Croissance du chiffre d\u2019affaires 3 ans', 'CA 3 ans'],
    croissance_resultat_3a: ['Croissance du résultat opérationnel 3 ans', 'Rés. op. 3 ans'],
    marge_operationnelle: ['Marge opérationnelle', 'Marge op.'],
    tendance_marge_3a: ['Tendance de la marge sur 3 ans', 'Tend. marge'],
    rentabilite_capitaux: ['Rentabilité des capitaux employés', 'ROCE'],
    conversion_tresorerie: ['Conversion en trésorerie', 'Conv. tréso.'],
    momentum_12_1: ['Momentum 12 mois hors dernier mois', 'Mom. 12 m'],
    momentum_6_1: ['Momentum 6 mois hors dernier mois', 'Mom. 6 m'],
    ecart_moyenne_200j: ['Écart à la moyenne 200 jours', 'Éc. 200 j'],
    ve_sur_resultat_op: ['Valeur d\u2019entreprise sur résultat opérationnel', 'VE / rés. op.'],
    rendement_flux_libre: ['Rendement du flux de trésorerie libre', 'Rend. FTL'],
    cours_sur_benefice: ['Cours sur bénéfice', 'Cours / bén.'],
    dette_nette_sur_ebitda: ['Dette nette sur EBITDA', 'Dette / EBITDA'],
    couverture_interets: ['Couverture des intérêts', 'Couv. intérêts'],
    dilution_3a: ['Dilution du capital sur 3 ans', 'Dilution'],
    revision_consensus_3m: ['Révision du consensus sur 3 mois', 'Rév. 3 m'],
    revisions_nettes: ['Révisions nettes', 'Rév. nettes']
  };

  var PAYS = {
    'France': 'France', 'Germany': 'Allemagne', 'Netherlands': 'Pays-Bas', 'Belgium': 'Belgique',
    'Portugal': 'Portugal', 'Luxembourg': 'Luxembourg', 'Denmark': 'Danemark', 'Ireland': 'Irlande',
    'Italy': 'Italie', 'Bulgaria': 'Bulgarie', 'Austria': 'Autriche', 'Spain': 'Espagne',
    'Finland': 'Finlande', 'Sweden': 'Suède', 'Norway': 'Norvège', 'Poland': 'Pologne',
    'Greece': 'Grèce', 'Czechia': 'Tchéquie', 'Hungary': 'Hongrie', 'Estonia': 'Estonie',
    'Lithuania': 'Lituanie', 'Latvia': 'Lettonie', 'Slovenia': 'Slovénie', 'Slovakia': 'Slovaquie',
    'Croatia': 'Croatie', 'Romania': 'Roumanie', 'Malta': 'Malte', 'Cyprus': 'Chypre',
    'Iceland': 'Islande', 'Switzerland': 'Suisse', 'United Kingdom': 'Royaume-Uni',
    'United States': 'États-Unis'
  };

  /* Les métriques pénalisées sont exportées avec les identifiants courts du moteur. */
  var ALIAS = {
    rev_cagr_3y: 'croissance_ca_3a', opinc_cagr_3y: 'croissance_resultat_3a',
    op_margin: 'marge_operationnelle', op_margin_trend_3y: 'tendance_marge_3a',
    roce: 'rentabilite_capitaux', cash_conversion: 'conversion_tresorerie',
    mom_12_1: 'momentum_12_1', mom_6_1: 'momentum_6_1', dist_sma200: 'ecart_moyenne_200j',
    ev_ebit: 've_sur_resultat_op', fcf_yield: 'rendement_flux_libre', pe: 'cours_sur_benefice',
    nd_ebitda: 'dette_nette_sur_ebitda', interest_cover: 'couverture_interets',
    dilution_3y: 'dilution_3a', eps_rev_3m: 'revision_consensus_3m', net_revisions: 'revisions_nettes'
  };

  var DRAPEAUX = {
    dette_elevee: 'Dette élevée',
    dilution: 'Dilution',
    marge_en_baisse: 'Marge en baisse',
    faible_conversion_tresorerie: 'Faible conversion en trésorerie',
    revisions_en_baisse: 'Révisions en baisse',
    consensus_absent: 'Consensus absent',
    interets_minoritaires_absents: 'Intérêts minoritaires absents',
    sans_charge_d_interet: 'Aucune charge d’intérêt déclarée',
    ecart_d_exercices_irregulier: 'Écart d’exercices irrégulier',
    resultat_net_non_positif: 'Résultat net non positif',
    resultat_operationnel_non_positif: 'Résultat opérationnel non positif'
  };

  var RAISONS = {
    illiquide: 'Liquidité insuffisante',
    secteur_exclu: 'Secteur exclu du périmètre',
    non_eligible_pea: 'Non éligible au PEA',
    comptes_trop_anciens: 'Comptes trop anciens',
    symbole_non_resolu: 'Symbole non résolu',
    capitalisation_insuffisante: 'Capitalisation insuffisante',
    flux_negatifs_trois_ans: 'Flux de trésorerie négatifs trois ans de suite',
    endettement_excessif: 'Endettement excessif',
    endettement_sans_ebitda: 'Endettement sans EBITDA calculable',
    donnees_insuffisantes: 'Données insuffisantes',
    liquidite_inconnue: 'Liquidité inconnue',
    classe_d_actions_doublon: 'Classe d’actions en doublon'
  };

  function raison(r) {
    if (RAISONS[r]) return RAISONS[r];
    if (r && r.indexOf('pea_conflict') === 0) {
      var det = r.split(':')[1];
      return 'Conflit d’éligibilité PEA' + (det ? ' (' + det.trim() + ')' : '');
    }
    return r;
  }

  function metriqueLib(k) {
    var c = ALIAS[k] || k;
    return (METRIQUES[c] || [k])[0];
  }

  function nb(v, d) {
    if (v === null || v === undefined || isNaN(v)) return TIRET;
    return v.toLocaleString('fr-FR', { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function metrique(m) {
    if (!m || m.valeur === null || m.valeur === undefined) return TIRET;
    var v = m.valeur;
    if (m.unite === 'pourcent') return nb(v * 100, 1) + ' %';
    if (m.unite === 'points') return (v > 0 ? '+' : '') + nb(v * 100, 1) + ' pt';
    return nb(v, 2) + ' \u00d7';
  }

  function eur(v) {
    if (v === null || v === undefined) return TIRET;
    if (Math.abs(v) >= 1e9) return nb(v / 1e9, 2) + ' Md €';
    if (Math.abs(v) >= 1e6) return nb(v / 1e6, 1) + ' M€';
    if (Math.abs(v) >= 1e3) return nb(v / 1e3, 0) + ' k€';
    return nb(v, 0) + ' €';
  }

  function dateFr(s) {
    if (!s) return TIRET;
    var d = new Date(s.length === 10 ? s + 'T12:00:00Z' : s);
    if (isNaN(d)) return s;
    return d.toLocaleDateString('fr-FR', { day: '2-digit', month: 'long', year: 'numeric' });
  }

  function heureFr(s) {
    if (!s) return TIRET;
    var d = new Date(s);
    if (isNaN(d)) return s;
    return d.toLocaleDateString('fr-FR', { day: '2-digit', month: 'short', year: 'numeric' }) +
      ' à ' + d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
  }

  function charger() {
    return fetch('data.json')
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error('http')); })
      .catch(function () {
        if (window.DONNEES_INTEGREES) return window.DONNEES_INTEGREES;
        return Promise.reject(new Error('aucune source'));
      });
  }

  function themeActuel() {
    try { return localStorage.getItem('screener-theme') || 'auto'; } catch (e) { return 'auto'; }
  }
  function appliquerTheme(t) {
    var r = document.documentElement;
    if (t === 'auto') r.removeAttribute('data-theme'); else r.setAttribute('data-theme', t);
    try { localStorage.setItem('screener-theme', t); } catch (e) {}
  }
  function themeSuivant() {
    var t = themeActuel();
    var s = t === 'auto' ? 'clair' : t === 'clair' ? 'sombre' : 'auto';
    appliquerTheme(s);
    return s;
  }
  var t0 = themeActuel();
  if (t0 !== 'auto') document.documentElement.setAttribute('data-theme', t0);

  window.SCR = {
    TIRET: TIRET, SECTEURS: SECTEURS, BLOCS: BLOCS, BLOCS_COURT: BLOCS_COURT, POIDS: POIDS,
    METRIQUES: METRIQUES, DRAPEAUX: DRAPEAUX, RAISONS: RAISONS,
    PAYS: PAYS, ALIAS: ALIAS,
    nb: nb, metrique: metrique, eur: eur, dateFr: dateFr, heureFr: heureFr, charger: charger,
    raison: raison, metriqueLib: metriqueLib,
    // Un secteur ou un pays absent reste absent : il porte un libellé explicite,
    // jamais une chaîne vide qui le confondrait avec une donnée connue.
    secteur: function (s) { return s ? (SECTEURS[s] || s) : '(inconnu)'; },
    pays: function (p) { return p ? (PAYS[p] || p) : '(inconnu)'; },
    themeActuel: themeActuel, appliquerTheme: appliquerTheme, themeSuivant: themeSuivant
  };
})();
