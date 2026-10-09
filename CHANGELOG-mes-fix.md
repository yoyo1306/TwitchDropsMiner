# Changelog — branche `mes-fix` (par yoyo1306)

Basé sur [Chaython/TwitchDropsMiner](https://github.com/Chaython/TwitchDropsMiner)
commit `e739aff` (+ PR #1180 : login SMARTBOX, catalogue Sunkwi).

Correctifs personnels testés sur Windows 11. Pour réappliquer après un
`git pull` : `git apply` avec le diff, ou cherry-pick du commit.

## Auth (`twitch.py`)
- Garde-fou sur la réponse du device-flow : si Twitch ne renvoie pas de
  `device_code` (ex. `{"status":400,"message":"invalid client"}`), lève une
  erreur de login lisible au lieu du `KeyError: 'device_code'` obscur.
- `fetch_campaigns` : ignore les campagnes dont `dropCampaign` est `null`
  (champ gaté/inéligible) avec un warning, au lieu de crasher sur `None["id"]`.

## Progression des drops (`channel.py`, `twitch.py`) — v2, 9 oct 2026
Depuis le ~8 oct 2026, Twitch ne crédite plus les minutes envoyées par la seule
télémétrie `minute-watched` (réponse 204 mais inventaire figé). Correctif porté
de [rangermix/TwitchDropsMiner PR #164](https://github.com/rangermix/TwitchDropsMiner/pull/164)
(issue [#163](https://github.com/rangermix/TwitchDropsMiner/issues/163)) :
- Toutes les ~10 s : lecture de la playlist HLS de la chaîne regardée, puis
  requête `HEAD` sur **chaque** nouveau segment (aucun flux audio/vidéo
  téléchargé). Segments dédupliqués (cache borné à 256), URL de playlist
  rafraîchie si expirée (401/403/404), requêtes bornées par des timeouts.
- Playlist master parsée proprement (`#EXT-X-STREAM-INF`), au lieu de prendre
  la dernière ligne brute de la réponse.
- La télémétrie spade/beacon reste envoyée, au plus une fois par minute, mais
  ne compte plus comme preuve de visionnage.
- Plus d'estimation locale (`bump_minutes`) : la barre n'avance que sur une
  confirmation Twitch (websocket ou `CurrentDrop`, vérifié ~1 fois par minute).

## Watch (`channel.py`)
- `spade.twitch.tv` → `beacon.twitch.tv` : contourne les DNS/adblockers qui
  sinkholent `spade.twitch.tv` en `0.0.0.0` (même edge analytics Twitch).
- Échec d'extraction de l'URL spade : saute le cycle avec un warning au lieu
  de tuer toute l'application (`MinerException` rattrapée dans `send_watch`).

## Claims (`inventory.py`)
- Log la vraie erreur GQL en cas d'échec de claim (diagnostic, ex. integrity).

## Inventaire (`gui.py`)
- Molette ignorée quand tout tient déjà à l'écran (vertical + horizontal).
- Barres de scroll auto-masquées quand inutiles, resynchronisées après
  rétrécissement de la zone (ex. décochage d'un filtre).
- Anneau de focus du Canvas supprimé (`highlightthickness=0`) : il rognait
  4px et empêchait `xview` de valoir exactement `(0.0, 1.0)`.
- Pas vertical dynamique (max mesuré) au lieu des 156px forcés : fini le bas
  des cartes coupé ; recalculé uniquement quand la liste change (évite toute
  boucle reset → remesure → relayout qui figeait l'UI).
- Molette Linux : prise en charge de Button-4/5 (en plus de `<MouseWheel>`).
- Filtre : « Exclu » gagne toujours sur « Prioritaire » (parenthésage explicite).
