# Navod pro Jiriho: jak ma program premyslet o praci s titulem

Toto neni technicka implementace. Je to lidsky popis toho, jak by se appka mela
chovat, aby pri praci s jednim filmem nebo serialem neztracela kontext.

Soucasny stav:

- appka uz umi ratingy, watched, move/copy mezi seznamy a detail titulu,
- ale vztahy mezi seznamy jeste nejsou sjednocene jednim pravidlovym modelem.

Tento dokument popisuje cilove chovani.

## Hlavni princip

Kdyz stojis na jednom titulu, program te nema nutit dokoncit vse jednim
kliknutim.

Naopak ma fungovat takto:

1. otevres detail titulu,
2. postupne na nem delas zmeny,
3. klidne si odbocis na herce, rezisera nebo jinou souvisejici stranku,
4. vratis se zpet,
5. pokracujes tam, kde jsi skoncil,
6. teprve potom se doriesi, co ma byt watched, co ma zmizet ze seznamu a co ma
   zustat.

## Co to znamena v praxi

### Rating nema hned vsechno rozbit

Kdyz filmu das hodnoceni, jeste to neznamena, ze uz jsi definitivne skoncil se
vsemi dalsimi kroky kolem toho titulu.

Typicky priklad:

- das rating,
- pak se podivas na herce,
- vratis se na film,
- zkopirujes ho do `Kouknout znovu`.

Program si ma pamatovat, ze porad pracujes se stejnym titulem.

### Prace s titulem ma byt jako jedna mensi session

Detail titulu nema byt jen stranka s jednotlivymi izolovanymi tlacitky.

Ma to byt spis:

- `pracuju na tomto titulu`
- `postupne upravuju jeho stav`
- `az na konci se spocita vysledek`

To je dulezite hlavne kvuli temto situacim:

- rating muze znamenat watched,
- watched muze znamenat odstraneni z nekterych listu,
- ale nektere listy se naopak maji zachovat, napr. `Kouknout znovu`,
- `Plex Library` neni bezny watchlist a nema se chovat stejne.

## Cemu se chceme vyhnout

Nechceme, aby aplikace delala po kazdem kliku nevratne doménové rozhodnuti.

Spatny priklad:

- das rating ve `Koukni rychle`,
- appka te hned bez dalsiho vyhodi z celeho kontextu,
- z listu se titul ztrati,
- pak teprve zjistis, ze jsi ho chtel jeste dat do `Kouknout znovu`.

To je presne ten typ chovani, ktery ma zmizet.

## Jaky je cilovy vysledek

Po dokonceni prace s titulem ma byt vysledek konzistentni:

- titul muze byt watched,
- muze mit rating,
- muze zustat v `Kouknout znovu`,
- muze zmizet z `Koukni rychle`,
- muze zaroven zustat v `Plex Library`,
- a to vsechno bez toho, aby ses musel bat, ze se pri mezikroku neco ztrati.

## Jak to souvisi s budoucim manualem

Jestli se tento model potvrdi, dava smysl z nej pozdeji udelat normalni napovedu
v appce nebo zaklad celeho manualu.

Prakticke casti manualu by pak mohly byt:

- `Jak funguje watched`
- `Jak funguje rating`
- `Rozdil mezi Move to a Copy to`
- `Proc nektere seznamy po watched zmizi a jine zustanou`
- `Co znamena Plex Library`
- `Jak funguje Kouknout znovu`

## Co jeste neni rozhodnute

Tento dokument zatim nezamyká tyto veci:

- jestli se ma zmena uzavirat explicitnim `Save`,
- jestli se ma title session drzet jen na detailu nebo i mezi navraty z detailu
  osoby,
- jak presne bude vypadat pravidlovy TOML soubor.

To se ma doplnit az po sepsani vice konkretnich scenaru.
