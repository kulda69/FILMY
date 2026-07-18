## Suggestion
- toho se bude hlavne tykat tato vetev uvazovani a pozdeji doufam i funkce programu
- doporucovani filmu ti zatim moc nejde, ale nesoudim te
- napadlo me to toho zapojit chatgpt, jenze to bych musel platit API a na to zatim nemam
- je tu ale jedno dalsi reseni, ty by si pripravil filmy ktere se mi libily a treba ty ktere ne
- dostali bychom to do projektu /Volumes/not_inserted/AI/filmy-knihy (klidne si ho prohledni)
- tam by chatgpt provedl nejakou generalizaci toho co se mi libi
- a na zaklade toho by online vyhledal navrh na to co by se mi mohlo libit
- to neznamena ze skorovaci mechanismum by tady byl vypnuty, ten by spolupracoval
- pripadne by potom chatgpt mohl zpetne vratit jako json navrhy a tady by se zpracovaly

# Technicke reseni na strane tohoto projektu
- zakladnim predpokladem je abych mohl k filmu zapsat sve hodnoceni i slovnem
- predpokladam ze ty moje hodnoceni ukladas v nejake tabulce, tu by bylo potreba rozsirit o dalsi jedno nebo 2 pole
- ty rozhodnes jestli bude stacit 1 pole podle nasledujicicho - mel bych zadat pozitiva filmu a take bych mel zadat co se mi nelibilo
- to by bylo soucasti tech dat ktera by pouzival chatgpt
- jedna moznost je ze bys mi vyexportoval nejaky json soubor a ten by si chatgpt mohl zpracovat
- to se mi ale nelibi, takze cilem je vytvorit nejaky endpoit vracejici json soubor a chatgpt si ten endpoint zavo sam pres api
- predpokladam ze takovych endpointu bude vice protoze:
  - jak urcit ktere filmy predat chatgptu
  - watchlist? nesmysl
  - filmy s vysokym hodnocenim? mozna
  - ale zacal bych treba filmy ze seznamu Kouknout znovu
  - tohle je jako zacatek

# strana chatgptu
- to je samostatny projekt ktery tady prilis resit nebudu jen
  - musime rozhodnout ktere informace chatgptu predame urcite to bude id imdb a id tmdb
  - mame vubec k dispozici id tmdb?
  - dale by tam pro jistotu mel byt nazev filmu
  - moje hodnoceni v tomto programu
  - moje slovni hodnoceni
  - a mozna jake to ma vypocitane skore
  - a určite genre
  - mozna affinuty vuci herci
  - a nejaky zpusobem jak bylo do skore zapocitano Favorite Genres a Favorite Traits
  - 
 

