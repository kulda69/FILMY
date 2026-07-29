# Navrh: rule builder pro list actions

Tento soubor navazuje na:

- [LIST_ACTIONS_AND_TITLE_SESSION.md](LIST_ACTIONS_AND_TITLE_SESSION.md)
- [LIST_ACTIONS_SCENARIO_MATRIX.md](LIST_ACTIONS_SCENARIO_MATRIX.md)
- [MANUAL_TITLE_WORKFLOW_DRAFT.md](MANUAL_TITLE_WORKFLOW_DRAFT.md)

Cil:

- prejit od rucne rozepsanych scenaru k obecnejsimu modelu pravidel,
- drzet pevnou sadu typu akci a typu efektu,
- umoznit jednotne UI pro vsechny listy,
- a zaroven mit dost pevny model, aby sel dobre validovat i vykonavat v kodu.

Tohle zatim neni finalni schema. Je to navrh datoveho modelu.

## Hlavni myslenka

Pravidlo nema byt "specialni scenar pro Watchlist", ale skladba kroku:

1. uzivatel nebo system vyvola `trigger_action`
2. pravidlo zkontroluje kontext
3. vykona jeden nebo vice `effect_step`
4. kdyz je potreba, navaze dalsim krokem

Priklad:

- `trigger_action = set_rating`
- `effect_step = derive_watched`
- `effect_step = write_watched`
- `effect_step = deactivate_source_membership`

nebo:

- `trigger_action = copy_to_list`
- `target_list = stahnout`
- `effect_step = add_target_membership`
- `effect_step = preserve_source_membership`

## Pevne typy trigger akci

Tohle ma byt kontrolovany enum, ne volny text.

- `set_rating`
- `mark_watched`
- `copy_to_list`
- `move_to_list`
- `remove_from_list`
- `set_notes`

Poznamka:

- `set_rating` a `mark_watched` jsou akce bez cile
- `copy_to_list` a `move_to_list` jsou akce s cilem

## Pevne typy effect kroku

I effecty maji byt kontrolovany enum.

- `write_rating`
- `derive_watched`
- `write_watched`
- `add_target_membership`
- `remove_source_membership`
- `deactivate_source_membership`
- `preserve_source_membership`
- `preserve_target_membership`
- `remove_target_membership`
- `noop`

Mozny pozdejsi doplnek:

- `require_finalize`
- `stop_processing`

## Jeden radek pravidla

Pracovni model:

```json
{
  "rule_id": "watchlist:set_rating:001",
  "source_list_id": "watchlist",
  "source_role": "planned",
  "trigger_action": "set_rating",
  "target_mode": "none",
  "target_list_id": null,
  "target_role": null,
  "effect_type": "derive_watched",
  "phase": "immediate",
  "effect_params": {},
  "order_index": 10,
  "enabled": true
}
```

Vysvetleni poli:

- `rule_id`
  stabilni technicke ID radku pravidla

- `source_list_id`
  konkretni list, nad kterym se pravidlo edituje

- `source_role`
  doménová role listu v dobe vytvoreni pravidla; muze slouzit jako pomocna
  kontrola nebo fallback

- `trigger_action`
  jedna z pevnych akci

- `target_mode`
  `none`, `specific_list`, `any_list`, `specific_role`

- `target_list_id`
  vyplnene jen kdyz akce ma cil a pravidlo miri na konkretni list

- `target_role`
  vyplnene jen kdyz pravidlo miri na cilovou roli misto konkretniho listu

- `effect_type`
  jeden z pevnych efektu

- `phase`
  `immediate` nebo `finalize_only`

- `effect_params`
  doplnkove parametry pro budouci rozsirovani

- `order_index`
  poradi kroku v ramci jednoho pravidloveho retezce

- `enabled`
  umozni pravidlo dočasně vypnout bez smazani

## Skupina pravidel pro jednu akci

V UI muze jeden uzivatelsky scenar vypadat jako vic radku pod sebou.

Priklad pro `Watchlist` a `set_rating`:

```json
[
  {
    "rule_id": "watchlist:set_rating:001",
    "source_list_id": "watchlist",
    "trigger_action": "set_rating",
    "target_mode": "none",
    "effect_type": "derive_watched",
    "phase": "immediate",
    "order_index": 10
  },
  {
    "rule_id": "watchlist:set_rating:002",
    "source_list_id": "watchlist",
    "trigger_action": "set_rating",
    "target_mode": "none",
    "effect_type": "write_watched",
    "phase": "immediate",
    "order_index": 20
  },
  {
    "rule_id": "watchlist:set_rating:003",
    "source_list_id": "watchlist",
    "trigger_action": "set_rating",
    "target_mode": "none",
    "effect_type": "deactivate_source_membership",
    "phase": "finalize_only",
    "order_index": 30
  }
]
```

Priklad pro `Watchlist` a `copy_to_list -> Stahnout`:

```json
[
  {
    "rule_id": "watchlist:copy_to_list:001",
    "source_list_id": "watchlist",
    "trigger_action": "copy_to_list",
    "target_mode": "specific_list",
    "target_list_id": "trakt-list-25844042",
    "effect_type": "add_target_membership",
    "phase": "finalize_only",
    "order_index": 10
  },
  {
    "rule_id": "watchlist:copy_to_list:002",
    "source_list_id": "watchlist",
    "trigger_action": "copy_to_list",
    "target_mode": "specific_list",
    "target_list_id": "trakt-list-25844042",
    "effect_type": "preserve_source_membership",
    "phase": "finalize_only",
    "order_index": 20
  }
]
```

## Navrh UI modelu

Kazdy list ma mit stejne viditelne akce.

Jeden radek editoru:

- dropdown `trigger action`
- volitelny dropdown `target list`
- dropdown `effect`

Pravidla pro UI:

- kdyz `trigger_action` nema cil, pole `target list` je disabled
- kdyz `trigger_action` cil ma, pole `target list` je povinne
- `phase` je povinne a uzivatel ma vzdy vedet, jestli se krok deje hned nebo az
  pri finalize
- kdyz kombinace nedava smysl, radek zustane viditelny, ale je locked; po
  kliknuti se ma ukazat duvod, proc je zamceny
- po vyberu validniho effectu se automaticky muze objevit dalsi prazdny radek

To znamena, ze editor nepracuje s jednim obrovskym scenarem, ale s radky:

- `set_rating -> derive_watched`
- `set_rating -> write_watched`
- `set_rating -> archive_source_membership`

## Co ma jit osetrit v kodu

Prave diky pevným typum akci a efektu pujde udelat dvoustupnova kontrola.

### 1. Validace editoru

Priklady:

- `set_rating` nesmi mit `target_list_id`
- `copy_to_list` musi mit `target_list_id`
- `move_to_list` nesmi pouzit `preserve_source_membership` jako jediny effect
- `deactivate_source_membership` nedava smysl bez `source_list_id`
- `write_rating` nema smysl ve `finalize_only`
- `add_target_membership` bez specialni vyjimky nema byt `immediate`

### 2. Validace backendu

Backend ma stejne kontroly zopakovat, i kdyby UI neco pustilo spatne.

Priklady:

- `target_list_id` musi existovat
- `source_list_id` musi existovat
- `trigger_action` musi byt z povoleneho enumu
- `effect_type` musi byt z povoleneho enumu
- `phase` musi byt `immediate` nebo `finalize_only`
- `order_index` v ramci jedne akce nesmi kolidovat

## Vazba na finalize

Tenhle rule builder sam o sobe jeste neresi, co se zapisuje hned a co az pri
`finalize_title_session`.

Ale vytvari dobry zaklad:

- kazdy radek pravidla ma explicitni `phase`
- `phase` rika, jestli se krok vykona hned nebo az pri `finalize_title_session`

Pracovni rozdeleni:

- `write_rating` = spis immediate
- `write_watched` = spis immediate
- `derive_watched` = immediate; i po jeho vyhodnoceni ale session musi dal
  drzet identitu titulu, aby nad nim slo pokracovat dalsimi akcemi jako
  `copy_to_list` nebo `move_to_list`
- `add_target_membership` = spis pending do finalize
- `deactivate_source_membership` = spis finalize
- `preserve_*` = finalize pravidlo

## Co z toho plyne pro dalsi DB navrh

Pokud se tenhle model potvrdi, bude potreba drzet oddelene:

1. definice pravidel
2. rozpracovanou `title_session`
3. execution plan pro konkretni session

Prvni hruby obrys:

- `list_action_rules`
- `title_sessions`
- `title_session_actions`
- `title_session_effect_queue`

## Uzavrene body po prvnim upresneni

- `target_list_id` ma byt vzdy konkretni list, ne role
- UI se ma editovat jen pro konkretni list, ne pro sablonu role
- kdyz akce nedava smysl, ma zustat viditelna, ale bude locked a po kliknuti
  ukaze duvod zamceni
- `derive_watched` ma byt `immediate`; zaroven ale session nesmi ztratit
  identitu titulu, aby po ratingu nebo watched slo dal provest `copy_to_list`
  nebo `move_to_list`
- `planned_download` muze koexistovat s `watched`; watched ani rating ho
  automaticky nerusi. Pokud uzivatel chce mit i zhlednuty titul ve `Stahnout`,
  je to validni stav. Teprve po skutecnem stazeni se ma titul explicitni akci
  presunout do `Plex Library` nebo `Mam`.
- `Watchlist` neni legitimni cil pro `copy_to_list` ani `move_to_list` z
  jakehokoli jineho seznamu. Do `Watchlist` se ma vstupovat jinou, primou akci
  typu watchlist toggle/pridani, ne obecnym list-action builderem.
- `AI navrhy` jsou docasny inbox s tlacitkem `Vycistit`, proto nema davat smysl
  je pres obecny builder posilat do `Watchlist`. Pokud si uzivatel chce neco z
  `AI navrhy` ponechat pred vycistenim, ma to jit do jineho stabilniho seznamu
  typu `Doporuceni`.

## Standardni duvody zamceni pro UI

Tyto duvody maji byt pevny kontrolovany seznam, ne volny text psany ad hoc.

Pracovni klice:

- `target_not_allowed_for_action`
- `target_same_as_source`
- `target_requires_special_entrypoint`
- `target_conflicts_with_list_purpose`
- `action_not_applicable_for_list`
- `action_requires_real_target`
- `action_conflicts_with_current_state`

Doporuceny uzivatelsky text:

| Klic | Kdy se pouzije | Doporuceny text |
| --- | --- | --- |
| `target_not_allowed_for_action` | cilovy list je pro danou akci obecne zakazany | `Tento cil pro danou akci neni povoleny.` |
| `target_same_as_source` | uzivatel miri do stejneho seznamu | `Titul uz v tomto seznamu je.` |
| `target_requires_special_entrypoint` | cil existuje, ale nema se resit pres obecny builder | `Tento seznam ma vlastni specialni akci a neumi se nastavovat pres obecny builder.` |
| `target_conflicts_with_list_purpose` | kombinace odporuje smyslu ciloveho seznamu | `Tato kombinace nedava pro vyznam ciloveho seznamu smysl.` |
| `action_not_applicable_for_list` | samotna akce neni pro tento list relevantni | `Tato akce pro tento seznam nedava smysl.` |
| `action_requires_real_target` | akce s cilem nema zvoleny konkretni list | `Nejdriv vyber konkretni cilovy seznam.` |
| `action_conflicts_with_current_state` | pravidlo by odporovalo uzavrenemu chovani jine akce | `Tato akce je v konfliktu s uz nastavenym chovanim.` |

## Prvni mapovani duvodu na aktualni rozhodnuti

- `copy_to_list` nebo `move_to_list` do `Watchlist`
  - duvod: `target_requires_special_entrypoint`
  - text: `Watchlist ma vlastni specialni akci a neumi se nastavovat pres obecny builder.`

- `AI navrhy -> Watchlist`
  - duvod: `target_requires_special_entrypoint`
  - text: `AI navrhy jsou docasny inbox; pokud si je chces ponechat, pouzij nejdriv stabilni seznam typu Doporuceni.`

- `owned_local -> planned_download`
  - duvod: `target_conflicts_with_list_purpose`
  - text: `Titul uz mas lokalne k dispozici, proto nedava smysl planovat jeho stazeni.`

- `copy_to_list` nebo `move_to_list` do stejneho seznamu
  - duvod: `target_same_as_source`
  - text: `Titul uz v tomto seznamu je.`

- akce s cilem bez vybraneho konkretniho seznamu
  - duvod: `action_requires_real_target`
  - text: `Nejdriv vyber konkretni cilovy seznam.`

- kombinace, kterou pozdeji zablokuje jina finalni logika
  - duvod: `action_conflicts_with_current_state`
  - text: `Tato akce je v konfliktu s uz nastavenym chovanim.`

## Uzavrene body po dalsim upresneni

- duvod zamceni se ma ukladat jako pevny klic plus konkretni text pro dany
  pripad; klic drzi konzistentni logiku a text dava uzivateli srozumitelne
  vysvetleni v UI
