# Navrh: databazovy model pro list actions a title session

Tento soubor navazuje na:

- [LIST_ACTIONS_AND_TITLE_SESSION.md](LIST_ACTIONS_AND_TITLE_SESSION.md)
- [LIST_ACTIONS_SCENARIO_MATRIX.md](LIST_ACTIONS_SCENARIO_MATRIX.md)
- [LIST_ACTION_RULE_BUILDER_DRAFT.md](LIST_ACTION_RULE_BUILDER_DRAFT.md)
- [MANUAL_TITLE_WORKFLOW_DRAFT.md](MANUAL_TITLE_WORKFLOW_DRAFT.md)

Cil:

- prevest domluveny rule-builder model do konkretnejsi PostgreSQL podoby,
- oddelit konfiguraci pravidel od bezici session nad jednim titulem,
- drzet zvlast `immediate` efekty a zvlast `finalize_only` efekty,
- a pripravit zaklad pro budouci implementaci bez hardcoded ifu podle nazvu seznamu.

Tohle porad neni finalni migrace. Je to technicky navrh dalsiho kroku.

## Hlavni rozdeleni

Budou existovat tri oddelene vrstvy:

1. konfigurace pravidel
2. bezici `title_session`
3. execution plan a audit vykonanych efektu

To je dulezite, protoze:

- pravidla jsou relativne stabilni konfigurace,
- session je kratkodoby pracovni kontext nad jednim titulem,
- a execution plan je konkretni vypocet pro jednu session, ne nova trvala definice pravidla.

## 1. Konfiguracni vrstva

### `app.list_action_rules`

Kazdy radek je jeden effect step.

Navrhovane sloupce:

- `rule_id uuid primary key`
- `source_list_id uuid not null references app.user_lists(id)`
- `trigger_action text not null`
- `target_list_id uuid null references app.user_lists(id)`
- `effect_type text not null`
- `phase text not null`
- `order_index integer not null`
- `enabled boolean not null default true`
- `lock_reason_key text null`
- `lock_reason_text text null`
- `effect_params jsonb not null default '{}'::jsonb`
- `created_at timestamptz not null default now()`
- `updated_at timestamptz not null default now()`

Poznamky:

- `source_role` sem zamerne nedavat jako autoritativni sloupec. Role listu uz zije v `app.user_lists`; pokud bude potreba audit, muze se pozdeji pridat jen jako snapshot.
- `target_list_id` zustava vzdy konkretni list, nikdy role.
- locked kombinace se muzou bud drzet primo jako disabled radek pravidla, nebo v samostatne validacni tabulce. Pro prvni implementaci je jednodussi nechat lock metadata primo tady.

Doporucene constraints:

- `trigger_action in ('set_rating', 'mark_watched', 'copy_to_list', 'move_to_list', 'remove_from_list', 'set_notes')`
- `effect_type in ('write_rating', 'derive_watched', 'write_watched', 'add_target_membership', 'remove_source_membership', 'deactivate_source_membership', 'preserve_source_membership', 'preserve_target_membership', 'remove_target_membership', 'noop')`
- `phase in ('immediate', 'finalize_only')`
- unikatni poradi: `(source_list_id, trigger_action, coalesce(target_list_id, '00000000-0000-0000-0000-000000000000'::uuid), phase, order_index)`

Minimalni indexy:

- `(source_list_id, trigger_action, enabled)`
- `(source_list_id, trigger_action, target_list_id, enabled)`

### `app.list_action_rule_sets`

Volitelna pomocna tabulka, pokud budeme chtit seskupovat vice radku do jednoho uzivatelskeho scenare.

Navrhovane sloupce:

- `rule_set_id uuid primary key`
- `source_list_id uuid not null references app.user_lists(id)`
- `trigger_action text not null`
- `target_list_id uuid null references app.user_lists(id)`
- `name text null`
- `enabled boolean not null default true`
- `created_at timestamptz not null default now()`
- `updated_at timestamptz not null default now()`

Pragmaticka poznamka:

- pro prvni verzi to muze byt zbytecne a muze se zacit jen s `list_action_rules` bez dalsi seskupovaci tabulky
- kdyz to pujde bez ni, je to lepsi

## 2. Session vrstva

### `app.title_sessions`

Jedna session reprezentuje jednu otevrenou praci nad jednim titulem.

Navrhovane sloupce:

- `session_id uuid primary key`
- `tconst text not null references app.imdb_titles(tconst)`
- `status text not null`
- `opened_from text null`
- `return_to_url text null`
- `source_list_id uuid null references app.user_lists(id)`
- `session_scope text not null default 'title_detail'`
- `started_at timestamptz not null default now()`
- `updated_at timestamptz not null default now()`
- `finalized_at timestamptz null`

Doporucene hodnoty:

- `status in ('open', 'finalizing', 'finalized', 'abandoned')`
- `session_scope in ('title_detail', 'list_row_menu', 'search_result', 'system_import')`

Smysl klicovych poli:

- `source_list_id` = aktivni zdrojovy list pro danou session; pro prvni verzi neresit zvlast `opened_list_id` a `current_list_id`
- `return_to_url` = UI navratovy kontext, ne domenova logika

### `app.title_session_actions`

Sem se zapisuje kazdy explicitni uzivatelsky krok.

Navrhovane sloupce:

- `action_id uuid primary key`
- `session_id uuid not null references app.title_sessions(session_id) on delete cascade`
- `tconst text not null`
- `source_list_id uuid null references app.user_lists(id)`
- `trigger_action text not null`
- `target_list_id uuid null references app.user_lists(id)`
- `rating_value smallint null`
- `notes_text text null`
- `action_payload jsonb not null default '{}'::jsonb`
- `action_order integer not null`
- `created_at timestamptz not null default now()`

Priklady:

- `set_rating`:
  - `rating_value = 8`
  - `target_list_id = null`

- `copy_to_list`:
  - `target_list_id = <Stahnout>`
  - `rating_value = null`

- `set_notes`:
  - `notes_text = 'silny finale a hudba'`

Dulezite:

- tady ma byt zaznamenan zamer uzivatele, ne kompletni domenove dusledky
- `action_order` urcuje poradi v session a pomuze pri debugovani i finalize

### `app.title_session_state`

Volitelna pomocna tabulka nebo materializovany snapshot, pokud se ukaze, ze je potreba prubezne drzet odvozeny pracovni stav session bez plneho re-read vsech akci.

Navrhovane sloupce:

- `session_id uuid primary key references app.title_sessions(session_id) on delete cascade`
- `derived_rating smallint null`
- `derived_watched boolean not null default false`
- `active_source_list_id uuid null references app.user_lists(id)`
- `state_payload jsonb not null default '{}'::jsonb`
- `updated_at timestamptz not null default now()`

Pragmaticka poznamka:

- pokud pujde prvni verze rozumne implementovat bez tehle tabulky, je lepsi ji zatim nechat stranou
- muze to byt jen interni Python/SQL projection pri finalize

## 3. Execution plan vrstva

### `app.title_session_effect_queue`

Tohle je nejdulezitejsi operacni vrstva.

Sem se zapisuje konkretni efekt, ktery vzesel z:

- akce uzivatele,
- odpovidajicich pravidel,
- a aktualniho stavu session.

Navrhovane sloupce:

- `effect_id uuid primary key`
- `session_id uuid not null references app.title_sessions(session_id) on delete cascade`
- `action_id uuid null references app.title_session_actions(action_id) on delete set null`
- `rule_id uuid null references app.list_action_rules(rule_id) on delete set null`
- `tconst text not null`
- `effect_type text not null`
- `phase text not null`
- `source_list_id uuid null references app.user_lists(id)`
- `target_list_id uuid null references app.user_lists(id)`
- `effect_status text not null`
- `effect_order integer not null`
- `effect_payload jsonb not null default '{}'::jsonb`
- `created_at timestamptz not null default now()`
- `executed_at timestamptz null`

Doporucene hodnoty:

- `effect_status in ('pending', 'applied', 'skipped', 'cancelled', 'failed')`

Smysl:

- `title_session_actions` rika, co chtel uzivatel
- `title_session_effect_queue` rika, co z toho system konkretne odvodil a ma vykonat

### `app.title_session_effect_log`

Volitelny auditni log, pokud nechceme po vykonani efektu prepisovat stejnou frontu.

Pro prvni verzi jsou dve moznosti:

1. drzet jen `title_session_effect_queue` a menit `effect_status`
2. nebo po vykonani kopirovat vysledek jeste do `effect_log`

Prakticky bych zacal variantou 1.

## Doporuceny beh session

### Krok 1: otevreni detailu

Vznikne nebo se obnovi `title_session`.

Zapis:

- radek do `title_sessions`

Zatim bez domenovych efektu.

### Krok 2: immediate akce

Uzivatel udela treba:

- `set_rating`
- `mark_watched`
- `set_notes`

Zapis:

- radek do `title_session_actions`
- vypocet odpovidajicich `immediate` efektu
- radky do `title_session_effect_queue`
- okamzite provedeni `immediate` efektu

Priklady immediate efektu:

- `write_rating`
- `derive_watched`

Tady je dulezite, ze:

- `derive_watched` je immediate
- ale session zustava otevrena
- titul tedy lze dal v te same session `copy_to_list` nebo `move_to_list`

### Krok 3: finalize-only akce

Uzivatel prida dalsi zamer, napr:

- `copy_to_list -> Stahnout`
- `move_to_list -> Plex Library`

Zapis:

- dalsi radek do `title_session_actions`
- dopocet `finalize_only` efektu do `title_session_effect_queue`

Tyhle efekty se zatim jen pripravi jako `pending`.

### Krok 4: finalize session

Pri `finalize_title_session(session_id)` se stane:

1. session se zamkne proti soubeznemu finalize
2. nactou se vsechny `pending` efekty
3. provedou se v definovanem poradi
4. kolidujici nebo redundantni efekty se oznaci `skipped`
5. session dostane `status = finalized`
6. zapise se `finalized_at`

## Doporucene poradi efektu pri finalize

Hrube poradi:

1. `add_target_membership`
2. `preserve_target_membership`
3. `preserve_source_membership`
4. `deactivate_source_membership`
5. `remove_source_membership`
6. `remove_target_membership`
7. `noop`

Prakticky smysl:

- nejdriv bezpecne pridavat
- potom oznacit preserve pravidla
- az nakonec odebirat nebo deaktivovat

To je bezpecnejsi proti situaci:

- rating oznaci watched
- nasledne se titul kopiruje do `Stahnout`
- a teprve pak se ma z puvodniho `Watchlist` deaktivovat

## Jak bude vypadat konkretni priklad

Priklad:

- zdrojovy list `Watchlist`
- uzivatel zada `set_rating = 9`
- nasledne udela `copy_to_list -> Stahnout`

Zapis do `title_session_actions`:

1. `set_rating`
2. `copy_to_list`

Zapis do `title_session_effect_queue`:

1. `write_rating` `immediate`
2. `derive_watched` `immediate`
3. `write_watched` `immediate`
4. `add_target_membership(Stahnout)` `finalize_only`
5. `preserve_source_membership(Watchlist)` nebo `deactivate_source_membership(Watchlist)` podle pravidla

Vysledek:

- rating a watched jsou zapsane hned
- `Stahnout` se propise az pri finalize
- `Watchlist` se vyresi az podle finalize pravidel

## Kde ma zit skutecna domenova zmena

Konfiguracni tabulky nemaji byt zdroj pravdy pro vsechno.

Skutecna uzivatelska data zustavaji tam, kde uz ziji dnes:

- rating v `app.user_ratings`
- watched v runtime/history vrstvach projektu
- membership seznamu v `app.user_list_items`

`title_session*` tabulky jsou orchestracni a auditni vrstva nad tim, ne nahrada stavajicich hlavnich tabulek.

## Potvrzene pracovni volby pro v1

- session drzi jeden aktivni `source_list_id`
- vychozi cleanup zdrojoveho clenstvi ma jit pres `deactivate_source_membership`
- `remove_source_membership` ma zustat jen jako pozdejsi specialni nebo vyjimecna akce, ne jako bezny default
- akce bez cile se propisuji jako `immediate`
- `write_watched` se pro v1 nema provadet hned; ma byt soucasti automatickeho finalize pri odchodu z detailu
- pro akce s cilem nema v1 mit extra potvrzovaci tlacitko; pokud session obsahuje `finalize_only` efekty, maji se automaticky finalizeovat pri odchodu z detailu

## Co podle me nema byt v prvni implementaci

- genericky engine pro role misto konkretniho `source_list_id`
- slozite dedeni pravidel podle role sablon
- automaticke cross-session obnovovani po dnech
- snaha nacpat do prvni verze timeouty, browser restore a viceuzivatelske edge casy

Prvni verze ma byt mensi:

- pravidla se edituji po konkretnim listu
- session plati pro jeden titul
- session drzi jeden aktivni `source_list_id`
- finalize je explicitni nebo navazany na jasny UI moment

## Doporuceny implementacni rez

Nejit vsechno najednou. Bezpecnejsi poradi:

1. zavedeni `app.list_action_rules`
2. zavedeni `app.title_sessions`
3. zavedeni `app.title_session_actions`
4. zavedeni `app.title_session_effect_queue`
5. prvni serverova funkce `app.finalize_title_session(session_id uuid)`
6. az potom napojeni konkretniho UI editoru pravidel

Tohle poradi dava smysl, protoze:

- nejdriv vznikne stabilni DB kostra,
- pak se da napsat jednoduchy backend smoke bez UI,
- a UI editor se muze stavet az nad overenym modelem.

## Otevrene body pro dalsi rez

V tomhle stavu je navrh pro v1 domluveny.
