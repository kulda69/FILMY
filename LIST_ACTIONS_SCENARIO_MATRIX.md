# Matice scenaru pro list actions a title session

Tento soubor je mezikrok mezi obecným návrhem
[LIST_ACTIONS_AND_TITLE_SESSION.md](LIST_ACTIONS_AND_TITLE_SESSION.md) a
budoucí implementací v databázi.

Cíl:

- rozepsat dnešní skutečné seznamy ve FILMY,
- u každé běžné akce zapsat očekávaný efekt,
- oddělit to, co se má uložit hned, od toho, co se má řešit až při
  `finalize_title_session`,
- připravit podklad pro první PostgreSQL návrh `title_session` /
  `pending_actions`.

Není to finální implementace ani uzamčené rozhodnutí. Je to pracovní matice
pro další technický návrh.

## Dnešní skutečné seznamy

| Název | ID nebo slug | Současný AI role signál | Navržená doménová role | Poznámka |
| --- | --- | --- | --- | --- |
| Watchlist | `watchlist` | `interested_planned` | `planned` | obecný plánovací seznam |
| Koukni rychle | `trakt-list-30606870` / `koukni-rychle` | `interested_planned` | `shortlist` | rychlý shortlist |
| Kouknout znou | `custom-list-66ceea59-6096-490d-ae20-a731da89e733` / `kouknout-znou` | `strong_positive` | `rewatch` | má se po watched typicky zachovat |
| Mam | `trakt-list-33338492` / `mam` | `interested_owned` | `owned_local` | titul už je lokálně k dispozici |
| Plex Library | `plex-library` | `interested_planned` | `owned_local` | pro AI je to slabší signál zájmu, ale pro list-actions to není běžný planning list a po watched/rating se má zachovat |
| Rozkoukáno | `custom-list-37605990-314c-45b2-8b52-2c42c0b41ca6` / `rozkoukano` | `in_progress` | `in_progress_list` | zvláštní list kolem rozpracovaného stavu |
| AI návrhy | `ai-suggestions` / `ai-navrhy` | `external_suggestion` | `external_suggestion` | výstupní inbox, ne vstup pro AI |
| Nedokoukáno | `custom-list-12777833-4ea6-407c-9ef4-e40909590ee8` / `nedokoukano` | `negative` | `negative` | negativní signál |
| Stáhnout | `trakt-list-25844042` / `stahnout` | `interested_planned` | `planned_download` | pořád planning, ale se silnějším operativním významem |

## Společné action signály

Pracovní anglické názvy akcí:

- `mark_watched`
- `set_rating`
- `move_to_list`
- `copy_to_list`
- `remove_from_list`
- `set_notes`

Pracovní odvozené signály:

- `derived_watched`
- `archive_membership`
- `preserve_membership`
- `pending_finalize`

## UI princip pro editaci vztahu list x action

Pro dalsi navrh plati jednotny princip:

- kazdy list ma v editoru vztahu videt stejnou sadu akci
- struktura editoru se nema menit list od listu
- akce, ktera pro dany list nedava smysl, nema zmizet, ale ma byt
  needitovatelna nebo vypnuta
- uzivatel tak ma videt cely model a zaroven hned pozna, ktere kombinace jsou
  povolene a ktere ne

Prakticky cil:

- neudelat z toho sadu special-case formularu
- drzet jednu konzistentni matici `list x action`
- validni kombinace jsou editovatelne
- nevalidni kombinace jsou viditelne, ale zamcene

Pracovní výklad:

- `immediate write` = bezpečné zapsat hned
- `finalize effect` = vyhodnotit až po celé session
- `preserve` = pravidlo s vyšší prioritou, které nesmí být později přepsané

## Matice podle zdrojového seznamu

### Watchlist

Role: `planned`

| Akce | Immediate write | Finalize effect | Preserve | Poznámka |
| --- | --- | --- | --- | --- |
| `mark_watched` | zapsat watch event | archivovat členství ve `planned` | ne | základní expected flow |
| `set_rating` | uložit rating | odvodit `derived_watched`, archivovat `planned` | ne | rating tu znamená dokoukáno |
| `move_to_list` -> `Koukni rychle` | pending membership change | odebrat z `Watchlist`, přidat do `Koukni rychle` | ne | čistý move bez watched efektu |
| `copy_to_list` -> `Koukni rychle` | pending membership change | ponechat `Watchlist`, přidat `Koukni rychle` | ano | nesmí samo zmizet z `Watchlist` |
| `copy_to_list` -> `Kouknout znou` | pending membership change | ponechat `Watchlist`, přidat `rewatch` | ano | pokud pak přijde rating, `rewatch` se zachová |

### Koukni rychle

Role: `shortlist`

| Akce | Immediate write | Finalize effect | Preserve | Poznámka |
| --- | --- | --- | --- | --- |
| `mark_watched` | zapsat watch event | archivovat členství v `shortlist` | ne | tohle je dnešní konkrétní problém |
| `set_rating` | uložit rating | odvodit `derived_watched`, archivovat `shortlist` | ne | stejné jako watchlist, ale nad shortlistem |
| `move_to_list` -> `Watchlist` | pending membership change | odebrat `shortlist`, přidat `planned` | ne | explicitní move má přednost |
| `copy_to_list` -> `Kouknout znou` | pending membership change | ponechat `shortlist`, přidat `rewatch` | ano | při pozdějším watched/rating zůstane `rewatch` |
| `copy_to_list` -> `Mam` | pending membership change | ponechat `shortlist`, přidat `owned_local` | ano | vlastnictví je vedlejší informace, ne watched |

### Kouknout znou

Role: `rewatch`

| Akce | Immediate write | Finalize effect | Preserve | Poznámka |
| --- | --- | --- | --- | --- |
| `mark_watched` | zapsat watch event | ponechat členství v `rewatch` | ano | watched nesmí zrušit rewatch zájem |
| `set_rating` | uložit rating | odvodit `derived_watched`, ponechat `rewatch` | ano | rating nesmí titul vyhodit z rewatch listu |
| `move_to_list` -> `Watchlist` | pending membership change | odebrat `rewatch`, přidat `planned` | ne | explicitní move ruší preserve |
| `copy_to_list` -> `Watchlist` | pending membership change | ponechat `rewatch`, přidat `planned` | ano | oba významy mohou existovat současně |

### Mam

Role: `owned_local`

| Akce | Immediate write | Finalize effect | Preserve | Poznámka |
| --- | --- | --- | --- | --- |
| `mark_watched` | zapsat watch event | ponechat členství v `owned_local` | ano | titul je stále stažený / vlastněný |
| `set_rating` | uložit rating | odvodit `derived_watched`, ponechat `owned_local` | ano | rating tu není signál k odstranění |
| `move_to_list` -> `Koukni rychle` | pending membership change | odebrat `owned_local`, přidat `shortlist` | ne | explicitní move přepisuje preserve |
| `copy_to_list` -> `Koukni rychle` | pending membership change | ponechat `owned_local`, přidat `shortlist` | ano | běžný případ: mám to a chci to pustit brzo |

### Plex Library

Role: `owned_local`

| Akce | Immediate write | Finalize effect | Preserve | Poznámka |
| --- | --- | --- | --- | --- |
| `mark_watched` | zapsat watch event | ponechat členství v `owned_local` | ano | nemá se chovat jako planning list |
| `set_rating` | uložit rating | odvodit `derived_watched`, ponechat `owned_local` | ano | přesně tvůj příklad |
| `copy_to_list` -> `Koukni rychle` | pending membership change | ponechat `owned_local`, přidat `shortlist` | ano | lokální dostupnost + priorita ke spuštění |
| `copy_to_list` -> `Kouknout znou` | pending membership change | ponechat `owned_local`, přidat `rewatch` | ano | lokální dostupnost + rewatch zájem |

### Rozkoukáno

Role: `in_progress_list`

| Akce | Immediate write | Finalize effect | Preserve | Poznámka |
| --- | --- | --- | --- | --- |
| `mark_watched` | zapsat watch event | pravděpodobně archivovat `in_progress_list` | ne | po dokoukání už nemá být rozkoukáno |
| `set_rating` | uložit rating | odvodit `derived_watched`, archivovat `in_progress_list` | ne | rating obvykle znamená, že už je hotovo |
| `copy_to_list` -> `Kouknout znou` | pending membership change | ponechat `in_progress_list`, přidat `rewatch` | ano | otázka: smí existovat ještě před dokoukáním |

### AI návrhy

Role: `external_suggestion`

| Akce | Immediate write | Finalize effect | Preserve | Poznámka |
| --- | --- | --- | --- | --- |
| `mark_watched` | zapsat watch event | spíš archivovat členství v `external_suggestion` | ne | inbox je pracovní, ne trvalý |
| `set_rating` | uložit rating | odvodit `derived_watched`, spíš archivovat `external_suggestion` | ne | po ohodnocení už titul není jen AI kandidát |
| `copy_to_list` -> `Watchlist` | pending membership change | ponechat `external_suggestion`, přidat `planned` | ano | otázka: zda se má inbox držet do ručního vyčištění |
| `move_to_list` -> `Watchlist` | pending membership change | odebrat `external_suggestion`, přidat `planned` | ne | explicitní převzetí z inboxu |

### Nedokoukáno

Role: `negative`

| Akce | Immediate write | Finalize effect | Preserve | Poznámka |
| --- | --- | --- | --- | --- |
| `mark_watched` | zapsat watch event | ponechat `negative` nebo jen podle explicitního rozhodnutí | ano | watched samo nema mazat negativní zkušenost |
| `set_rating` | uložit rating | odvodit `derived_watched`, ponechat `negative` | ano | negativní signál je důležitý i po dokoukání |
| `move_to_list` -> `Kouknout znou` | pending membership change | odebrat `negative`, přidat `rewatch` | ne | explicitní změna názoru |

### Stáhnout

Role: `planned_download`

| Akce | Immediate write | Finalize effect | Preserve | Poznámka |
| --- | --- | --- | --- | --- |
| `mark_watched` | zapsat watch event | archivovat `planned_download` | ne | po zhlédnutí už není co stahovat |
| `set_rating` | uložit rating | odvodit `derived_watched`, archivovat `planned_download` | ne | stejně jako planning list |
| `move_to_list` -> `Mam` | pending membership change | odebrat `planned_download`, přidat `owned_local` | ne | přechod z plánu do vlastnictví |

## Přeshraniční scénáře

### `set_rating` v `Koukni rychle`, potom `copy_to_list` do `Kouknout znou`

Immediate writes:

- uložit rating
- zapsat pending `copy_to_list`

Finalize:

- odvodit `derived_watched`
- archivovat `shortlist`
- zachovat `rewatch`

Preserve:

- `rewatch` má vyšší prioritu než obecné watched cleanup pravidlo

### `set_rating` v `Plex Library`

Immediate writes:

- uložit rating

Finalize:

- odvodit `derived_watched`
- ponechat `owned_local`

Preserve:

- `owned_local` nesmí být chápáno jako běžný planning list

### `mark_watched` z detailu bez zjevného zdrojového seznamu

Immediate writes:

- zapsat watch event

Finalize:

- uklidit jen ty membershipy, které jsou aktivní a patří do watched-cleanup rolí

Open point:

- když není `opened_from`, musí se cleanup opřít o skutečná aktivní členství titulu,
  ne o jednu předanou list hodnotu

## Interakce se vsemi cilovymi rolemi

Tahle vrstva doplnuje predchozi matici o situace, kdy uzivatel pri jedne
session neudela jen `mark_watched` nebo `set_rating`, ale soucasne i explicitni
`move_to_list` nebo `copy_to_list`.

Tohle je dulezite pravidlo:

- explicitni cilova akce uzivatele ma vyssi prioritu nez automaticky cleanup
  odvozeny z `derived_watched`

Jinymi slovy:

- kdyz uzivatel v jedne session rekne `copy_to Stahnout`, nema se to ztratit jen
  proto, ze rating mezitim odvodil `watched`
- automaticka pravidla maji uklizet jen to, co uzivatel explicitne
  nepredefinoval

### Pracovni priorita pravidel

1. explicitni `move_to_list`
2. explicitni `copy_to_list`
3. explicitni `remove_from_list`
4. odvozene `derived_watched`
5. obecny role-based cleanup

### Cílové role

| Cílová role | Význam | Jak se má chovat při souběhu s `derived_watched` |
| --- | --- | --- |
| `planned` | obecný plán | pokud titul současně vznikne jako watched, samotné přidání do `planned` je podezřelé a musí se potvrdit podle scénáře; není to preserve role |
| `shortlist` | chci to brzy pustit | stejně jako `planned`, watched ji obvykle přebíjí |
| `rewatch` | chci si to pustit znovu | preserve role, explicitní cíl se má zachovat |
| `owned_local` | mám to lokálně | preserve role, explicitní cíl se má zachovat |
| `in_progress_list` | rozkoukáno / práce v běhu | watched ji obvykle ruší, pokud uživatel výslovně neřeší speciální případ |
| `external_suggestion` | AI inbox | není preserve role; watched/rating ji může uklidit |
| `negative` | negativní zkušenost | preserve role, pokud ji uživatel výslovně nepřepisuje move akcí |
| `planned_download` | chci stáhnout | watched ji obvykle ruší, ale explicitní cíl v téže session má přednost minimálně do okamžiku finalize rozhodnutí |

## Matice source x action x target role

### Source `planned` (`Watchlist`)

| Akce | Cílová role | Immediate write | Finalize effect | Poznámka |
| --- | --- | --- | --- | --- |
| `copy_to_list` | `shortlist` | pending membership change | ponechat `planned`, pridat `shortlist` | bez watched efektu |
| `copy_to_list` | `rewatch` | pending membership change | ponechat `planned`, pridat `rewatch` | `rewatch` je preserve |
| `copy_to_list` | `owned_local` | pending membership change | ponechat `planned`, pridat `owned_local` | typicky `Mam` nebo `Plex Library` |
| `copy_to_list` | `planned_download` | pending membership change | ponechat `planned`, pridat `planned_download` | typicky `Stahnout` |
| `move_to_list` | `shortlist` | pending membership change | odebrat `planned`, pridat `shortlist` | explicitni move |
| `move_to_list` | `rewatch` | pending membership change | odebrat `planned`, pridat `rewatch` | explicitni move |
| `move_to_list` | `owned_local` | pending membership change | odebrat `planned`, pridat `owned_local` | explicitni move |
| `move_to_list` | `planned_download` | pending membership change | odebrat `planned`, pridat `planned_download` | explicitni move |
| `set_rating` + `copy_to_list` | `planned_download` | ulozit rating + pending copy | odvodit `derived_watched`, archivovat `planned`, zachovat `planned_download` | presne tvuj priklad `Watchlist -> Stahnout` |
| `set_rating` + `move_to_list` | `planned_download` | ulozit rating + pending move | odvodit `derived_watched`, odebrat `planned`, pridat `planned_download` | explicitni move ma prednost |
| `mark_watched` + `copy_to_list` | `planned_download` | watch event + pending copy | archivovat `planned`, zachovat `planned_download` | watched nesmi smazat explicitni cil |

### Source `shortlist` (`Koukni rychle`)

| Akce | Cílová role | Immediate write | Finalize effect | Poznámka |
| --- | --- | --- | --- | --- |
| `copy_to_list` | `rewatch` | pending membership change | ponechat `shortlist`, pridat `rewatch` | do finalize bez cleanupu |
| `copy_to_list` | `owned_local` | pending membership change | ponechat `shortlist`, pridat `owned_local` | typicky `Mam` |
| `copy_to_list` | `planned_download` | pending membership change | ponechat `shortlist`, pridat `planned_download` | typicky `Stahnout` |
| `set_rating` + `copy_to_list` | `planned_download` | ulozit rating + pending copy | odvodit `derived_watched`, archivovat `shortlist`, zachovat `planned_download` | explicitni copy ma prednost |
| `set_rating` + `copy_to_list` | `rewatch` | ulozit rating + pending copy | odvodit `derived_watched`, archivovat `shortlist`, zachovat `rewatch` | preserve role |
| `mark_watched` + `move_to_list` | `owned_local` | watch event + pending move | archivovat `shortlist`, pridat `owned_local` | move ma prednost |

### Source `owned_local` (`Mam`, `Plex Library`)

| Akce | Cílová role | Immediate write | Finalize effect | Poznámka |
| --- | --- | --- | --- | --- |
| `copy_to_list` | `shortlist` | pending membership change | ponechat `owned_local`, pridat `shortlist` | mam to a chci to pustit brzo |
| `copy_to_list` | `rewatch` | pending membership change | ponechat `owned_local`, pridat `rewatch` | preserve + preserve |
| `copy_to_list` | `planned_download` | pending membership change | ponechat `owned_local`, zvazit jestli `planned_download` vubec dava smysl | kandidat na zakazanou/needitovatelnou kombinaci |
| `set_rating` + `copy_to_list` | `shortlist` | ulozit rating + pending copy | odvodit `derived_watched`, ponechat `owned_local`, rozhodnout jestli `shortlist` ma po watched prezit | spis ne, pokud nema byt specialni vyjimka |
| `set_rating` + `copy_to_list` | `rewatch` | ulozit rating + pending copy | odvodit `derived_watched`, ponechat `owned_local`, zachovat `rewatch` | plne smysluplny pripad |

### Source `rewatch` (`Kouknout znou`)

| Akce | Cílová role | Immediate write | Finalize effect | Poznámka |
| --- | --- | --- | --- | --- |
| `copy_to_list` | `planned_download` | pending membership change | ponechat `rewatch`, pridat `planned_download` | chci si to znovu pustit a zaroven stahnout |
| `copy_to_list` | `owned_local` | pending membership change | ponechat `rewatch`, pridat `owned_local` | velmi prirozeny pripad |
| `set_rating` + `copy_to_list` | `planned_download` | ulozit rating + pending copy | odvodit `derived_watched`, zachovat `rewatch`, zachovat `planned_download` | explicitni cil + preserve |

### Source `external_suggestion` (`AI navrhy`)

| Akce | Cílová role | Immediate write | Finalize effect | Poznámka |
| --- | --- | --- | --- | --- |
| `move_to_list` | `planned` | pending membership change | odebrat `external_suggestion`, pridat `planned` | prevzeti z AI inboxu |
| `copy_to_list` | `planned` | pending membership change | ponechat `external_suggestion`, pridat `planned` | otazka, jestli to chceme povolit |
| `set_rating` + `move_to_list` | `rewatch` | ulozit rating + pending move | odvodit `derived_watched`, odebrat `external_suggestion`, zachovat `rewatch` | AI kandidata uzivatel prijal a pretypoval |

## Kandidati na needitovatelne kombinace v UI

Tohle nejsou definitivni zakazy, ale prvni kandidati na akce, ktere by v
jednotnem editoru byly viditelne, ale zamcene:

- `owned_local -> planned_download`
  Duvod: nedava smysl planovat stazeni neceho, co uz mam lokalne.

- `external_suggestion -> external_suggestion`
  Duvod: copy nebo move do stejne role nema smysl.

- `negative -> planned_download`
  Duvod: mozna smysl ma, ale je to podezrely konflikt a potrebuje zvlastni
  rozhodnuti.

- `in_progress_list -> planned`
  Duvod: pokud uz je titul rozkoukany, navrat do cisteho planu vypada jako
  specialni vyjimka.

## Co z matice plyne pro DB návrh

První zjevná hranice:

- `set_rating` a `mark_watched` se jeví jako bezpečné `immediate writes`
- vztahy mezi seznamy a jejich cleanup se mají řešit až při `finalize`
- `move_to_list` a `copy_to_list` je lepší nejdřív držet jako
  `pending_membership_changes`, ne je okamžitě domykat se všemi vedlejšími
  efekty

To znamená, že první PostgreSQL řez pravděpodobně bude chtít:

- tabulku `title_sessions`
- tabulku `title_session_actions`
- tabulku nebo JSON payload pro `pending_membership_changes`
- serverový krok `finalize_title_session(...)`

## Otevřené body před implementací

- jestli `set_rating` má vždy znamenat `derived_watched`
- jestli `AI návrhy` mají po ratingu/watched mizet automaticky, nebo až po
  explicitním převzetí / vyčištění
- jestli `Rozkoukáno` má být běžná preserve role, nebo cleanup role
- jestli `Mam` a `Plex Library` sdílejí stejnou doménovou roli, nebo potřebují
  dvě různé preserve varianty
