# Navrh: typy dostupnosti v FILMY

Tento soubor je pracovní návrh, jak v appce rozlišovat různé druhy
„dostupnosti“. Není to implementace ani finální datový model.

## Proč to řešit

Slovo `dostupnost` se v projektu začíná používat pro více různých věcí:

- oficiální stream/rent/buy provider v ČR,
- titul, který je lokálně stažený nebo přítomný v `Plex Library`,
- neověřený nález typu FastShare hit.

Kdyby se to smíchalo do jedné hodnoty `available = true`, začalo by být
nejasné:

- co přesně uživateli tvrdíme,
- odkud signál pochází,
- jestli jde o legální/oficiální provider,
- jestli je to jen měkký hint,
- jak se to má chovat ve filtrech.

## Základní princip

V appce nemá existovat jedna univerzální dostupnost, ale několik oddělených
signálů.

První návrh kategorií:

- `official_provider`
- `owned_local`
- `community_hint`

## 1. official_provider

To je oficiální dostupnost z TMDB/JustWatch providerů pro ČR.

Příklady:

- Disney+
- Netflix
- Max
- Apple TV
- rent
- buy

Co smíme tvrdit:

- `Dostupné v ČR`
- `Stream`
- `Půjčení`
- `Koupě`

Co z toho může vzniknout:

- ostrý filtr `Jen dostupné v ČR`
- jemnější filtr podle typu `stream/rent/buy/ads`

## 2. owned_local

To je informace, že titul je lokálně „u mě“, typicky přes `Plex Library` nebo
jiný vlastněný/stažený zdroj.

Tohle není totéž jako oficiální provider.

Co smíme tvrdit:

- `Máš lokálně`
- `V Plex Library`
- `Lokálně dostupné`

Co nesmíme tvrdit:

- `Dostupné v ČR` ve stejném významu jako TMDB provider

Proč:

- jde o jiný druh dostupnosti,
- je to osobní/library signál, ne veřejná katalogová dostupnost.

## 3. community_hint

To je měkký, neověřený hit z komunitního nebo hostingového zdroje typu
FastShare.

První plánovaný příklad:

- `fastshare_hit`

Co smíme tvrdit:

- `Zkus FastShare`
- `Na FastShare jsem našel něco podobného`
- `FastShare hit`

Co nesmíme tvrdit:

- `Film tam je`
- `Dostupné`
- `Lze přehrát`

Tento signál má být jen doplňková poznámka, ne potvrzená dostupnost.

## Navrhované chování v UI

Tyto vrstvy se nemají míchat do jedné řádky.

Doporučený směr:

### Oficiální dostupnost

- seznam providerů z TMDB/JustWatch
- může být zdrojem pro hlavní filtr

### Lokálně máš

- samostatná poznámka nebo badge
- např. `Plex Library`

### Další možnost

- samostatná poznámka
- např. `Zkus FastShare`

## Navrhované chování ve filtrech

První důležitá hranice:

- `official_provider` se může použít pro ostré filtry
- `community_hint` se nemá chovat jako ostrý filtr oficiální dostupnosti

To znamená:

- `Jen dostupné v ČR` má zatím filtrovat jen `official_provider`
- `owned_local` může později dostat vlastní filtr typu `Jen co mám lokálně`
- `community_hint` se má spíš jen zobrazovat jako pomocná informace

## Jak to souvisí se seznamy

`Plex Library` tu nefunguje jako provider, ale jako signál typu `owned_local`.

Tím pádem:

- `Plex Library` nepatří do stejné kategorie jako TMDB providery,
- pravidla mezi seznamy a pravidla dostupnosti se nesmí slít dohromady,
- ale v budoucnu se mohou potkat v jednom „availability panelu“ na detailu
  titulu.

## První pracovní datový obrys

Ne finální schema, jen návrh přemýšlení:

### official_provider

- `tconst`
- `source = tmdb_watch_provider`
- `country_code`
- `provider_type`
- `provider_name`
- `checked_at`

### owned_local

- `tconst`
- `source = plex_library`
- `label`
- `checked_at`

### community_hint

- `tconst`
- `source = fastshare`
- `query_text`
- `matched_url`
- `matched_label`
- `status = hit | no_hit | uncertain`
- `checked_at`

## Otevřené otázky

1. Má být `owned_local` odvozené jen ze seznamu `Plex Library`, nebo z obecnější
   runtime vrstvy „mám lokálně“?
2. Má mít FastShare hit cache a expiraci?
3. Má se `community_hint` dohledávat na vyžádání, nebo na pozadí?
4. Jak přesně má vypadat budoucí panel na detailu titulu?

## Doporučený další krok

Zatím neimplementovat FastShare ani nepředělávat existující TMDB provider model.

Nejdřív držet tyto tři kategorie jako návrhovou slovní zásobu:

- `official_provider`
- `owned_local`
- `community_hint`

Teprve potom řešit:

- konkrétní DB model,
- konkrétní UI panel,
- případnou FastShare integraci jako `hit-only` signál.
