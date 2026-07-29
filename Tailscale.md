# Tailscale

Poznámky k nasazení `FILMY` na `mac-mini` přes `LaunchAgent` + `Caddy` + `Tailscale`.

## Aktuální stav k 2026-07-22

- `FILMY` je na `mac-mini` nainstalované v `/Users/kulda/apps/FILMY`.
- Opravený `LaunchAgent` používá `deploy/cz.kulda.filmy.plist` s reálnou cestou `/Users/kulda/apps/FILMY`.
- Služba běží pod labelem `cz.kulda.filmy`.
- Lokální app běží na `127.0.0.1:8019`.
- Ověřená update sekvence na `mac-mini` je:
  1. `git pull --ff-only`
  2. `uv sync`
  3. `uv run filmy-upgrade-database`
  4. `launchctl kickstart -k gui/$(id -u)/cz.kulda.filmy`

## Caddy a Tailscale

- `Caddyfile` je na `mac-mini` v `/Users/kulda/apps/Caddyfile`.
- Včerejší konfigurace byla vedená jako port-per-app model:
  - jeden blok pro `8019` (`FILMY`)
  - jeden blok pro `8020` (jiná appka)
- Z continuity vrstvy vychází tento potvrzený tvar:

```caddyfile
https://mini.taildce711.ts.net:8019 {
    bind 100.124.124.95
    reverse_proxy 127.0.0.1:8019
}

https://mini.taildce711.ts.net:8020 {
    bind 100.124.124.95
    reverse_proxy 127.0.0.1:8020
}
```

- Smysl je držet explicitní port u každé appky místo subdomén nebo subpath routing.
- `FILMY` problém aktuálně není v reverse proxy targetu `127.0.0.1:8019`, ale v TLS vrstvě mezi `Caddy` a `Tailscale`.

## Důležitý kontext z dřívějšího nasazení `penize`

- Jiří si 2026-07-23 vybavil, že podobný problém už se řešil při nasazení `penize`.
- Pracovní hypotéza:
  - App Store varianta `Tailscale` měla nějaké omezení nebo nekompatibilitu.
  - Tehdy bylo potřeba něco doinstalovat nebo přejít na jinou variantu.
  - Pak se ukázalo, že používaná varianta už možná ani nepotřebovala vlastní `tailscale cert` workflow.
  - Mezitím se ale `Tailscale` znovu aktualizoval a přestalo to fungovat.
- Tuhle hypotézu zatím bereme jako užitečnou stopu, ne jako ověřený fakt.

## Starší ověřený závěr z `penize`

- V dřívějším deployment bloku pro `penize` byl ověřený funkční stav přes plain HTTP over Tailscale:

```caddyfile
http://mini.taildce711.ts.net {
    reverse_proxy 127.0.0.1:8020
}
```

- Zároveň tam bylo zapsané, že:
  - `tailscale cert mini.taildce711.ts.net` vracelo `500 Internal Server Error: your Tailscale account does not support getting TLS certs`
  - App Store / GUI varianta `Tailscale` už tehdy padala na `BundleIdentifiers.swift:47`
- To znamená, že musíme odlišit dvě věci:
  - jestli je rozbitá jen konkrétní HTTPS `.ts.net` cesta přes LocalAPI/socket
  - nebo jestli je rozbitý Tailscale runtime obecně

## Otevřený problém

- Ruční běh `caddy run --config /Users/kulda/apps/Caddyfile` padal při TLS handshaku.
- Chyba byla:

```text
Get "http://local-tailscaled.sock/...": dial unix /var/run/tailscaled.socket: connect: no such file or directory
```

- Na stroji běží appková varianta `/Applications/Tailscale.app`.
- `/usr/local/bin/tailscale` je symlink do té appky.
- `tailscale status` padá na `Tailscale/BundleIdentifiers.swift:47`.
- `ls /var/run/tailscaled.socket` potvrzovalo, že socket neexistuje.

## Závěr z včerejška

- `FILMY` je na `mac-mini` rozběhnuté.
- Oprava plist cesty byla správně.
- Blokující problém už není v appce ani v `LaunchAgentu`.
- Blokující problém je Tailscale runtime / LocalAPI socket, který `Caddy` potřebuje pro HTTPS nad `.ts.net`.

## Nejbližší další krok

- Na `mac-mini` opravit nebo nahradit Tailscale variantu tak, aby existoval použitelný LocalAPI socket pro `Caddy`.
- Pak znovu otestovat:

```bash
curl -vk https://mini.taildce711.ts.net:8019
```

## Navržený testovací postup na `mac-mini`

1. Ověřit, co je teď skutečně nainstalované:
   - `which tailscale`
   - `tailscale version`
   - `ls -l /usr/local/bin/tailscale`
   - `ls -l /Applications/Tailscale.app`
2. Ověřit, jestli existuje LocalAPI/socket:
   - `ls -l /var/run/tailscaled.socket`
   - `ps aux | rg '[T]ailscale|[t]ailscaled'`
3. Ověřit, jestli Tailscale CLI vůbec funguje:
   - `tailscale status`
   - `tailscale ip -4`
4. Ověřit dnešní Caddy konfiguraci:
   - `cat /Users/kulda/apps/Caddyfile`
   - `caddy validate --config /Users/kulda/apps/Caddyfile`
5. Rozlišit HTTP vs HTTPS:
   - `curl -vk http://mini.taildce711.ts.net:8019`
   - `curl -vk https://mini.taildce711.ts.net:8019`
6. Když HTTPS selže, ale HTTP projde:
   - potvrdit, že appka i Caddy route fungují
   - problém je čistě TLS / Tailscale LocalAPI vrstva

## Poznámka k dnešnímu stavu z tohoto workspace

- Dnes 2026-07-23 není z tohoto workspace dostupný mount `/Volumes/kulda/apps`, takže aktuální `Caddyfile` ani skript `install_caddy_launchd.sh` nešlo lokálně přečíst.
- Pro live ověření tedy potřebujeme přímo shell na `mac-mini` nebo znovu připojený mount.

## Live ověření z `mac-mini` 2026-07-23

- `which tailscale` vrací `/usr/local/bin/tailscale`
- `/usr/local/bin/tailscale` je symlink na:

```text
/Applications/Tailscale.app/Contents/MacOS/Tailscale
```

- `Tailscale.app` existuje v:

```text
/Applications/Tailscale.app
```

- `tailscale version`, `tailscale status` i `tailscale ip -4` všechny padají stejně:

```text
Tailscale/BundleIdentifiers.swift:47: Fatal error: The current bundleIdentifier is unknown to the registry
zsh: trace trap  tailscale ...
```

- Z běžících procesů jsou vidět jen GUI/app komponenty:

```text
/Applications/Tailscale.app/Contents/PlugIns/IPNExtension.appex/Contents/MacOS/IPNExtension
/Applications/Tailscale.app/Contents/MacOS/Tailscale
```

- `ls -l /var/run/tailscaled.socket` vrací:

```text
ls: /var/run/tailscaled.socket: No such file or directory
```

- Aktuální `Caddyfile` na `mac-mini` je skutečně:

```caddyfile
https://mini.taildce711.ts.net:8020 {
	bind 100.124.124.95
	reverse_proxy 127.0.0.1:8020
}

https://mini.taildce711.ts.net:8019 {
	bind 100.124.124.95
	reverse_proxy 127.0.0.1:8019
}
```

- `caddy validate --config /Users/kulda/apps/Caddyfile` vrací `Valid configuration`.
- Na stroji běží `caddy run --config /Users/kulda/apps/Caddyfile` pod `root`.

## Průběžný závěr po live ověření

- Konfigurace `Caddy` syntakticky sedí.
- Port-per-app model pro `8019` a `8020` je opravdu nasazený.
- Samotný `tailscale` CLI je aktuálně rozbitý už na úrovni binárky z `Tailscale.app`, ne až na úrovni konkrétního příkazu.
- Chybějící `/var/run/tailscaled.socket` stále potvrzuje, že `Caddy` nemá odkud brát LocalAPI pro `.ts.net` HTTPS obsluhu.
- Nejpravděpodobnější pracovní hypotéza je pořád stejná:
  - App Store / GUI varianta `Tailscale` je pro tenhle způsob provozu nevhodná nebo rozbitá po update
  - `FILMY` ani `Caddyfile` nejsou primární příčina

## Curl a metadata testy z `mac-mini` 2026-07-23

- Lokální backend `FILMY` odpovídá přímo na loopbacku:

```text
curl -vk http://127.0.0.1:8019
HTTP/1.1 200 OK
server: uvicorn
```

- Lokální backend druhé appky na `8020` odpovídá také:

```text
curl -vk http://127.0.0.1:8020
HTTP/1.1 200 OK
server: uvicorn
```

- HTTP přístup přes Tailscale hostname a port `8019` dojde až na `Caddy`, ale je odmítnutý jako plain HTTP na HTTPS listener:

```text
curl -vk http://mini.taildce711.ts.net:8019
HTTP/1.0 400 Bad Request
Client sent an HTTP request to an HTTPS server.
```

- To je důležitý důkaz, že:
  - DNS `mini.taildce711.ts.net` funguje
  - port `8019` na adrese `100.124.124.95` je otevřený
  - `Caddy` opravdu poslouchá na správném portu

- HTTPS handshake ale padá stejně pro `8019` i `8020`:

```text
curl -vk https://mini.taildce711.ts.net:8019
LibreSSL/3.3.6: error:1404B438:SSL routines:ST_CONNECT:tlsv1 alert internal error

curl -vk https://mini.taildce711.ts.net:8020
LibreSSL/3.3.6: error:1404B438:SSL routines:ST_CONNECT:tlsv1 alert internal error
```

- Tím se potvrzuje, že problém není specifický pro `FILMY`, ale pro společnou TLS vrstvu.

- Metadata aplikace potvrzují App Store build:

```text
mdls -name kMDItemAppStoreHasReceipt /Applications/Tailscale.app
kMDItemAppStoreHasReceipt = 1

defaults read /Applications/Tailscale.app/Contents/Info CFBundleIdentifier
io.tailscale.ipn.macos
```

## Aktuální závěr po dnešních testech

- `FILMY` backend je zdravý.
- `penize` backend je zdravý.
- `Caddy` bind/reverse proxy vrstva je z velké části zdravá, protože HTTP request dojde na HTTPS listener.
- Rozbitá je TLS integrace pro `.ts.net` hostname, společná pro oba porty.
- Kořen problému je velmi pravděpodobně App Store build `Tailscale.app` plus chybějící LocalAPI/socket, ne aplikace `FILMY`.

## Doporučený další krok

- Nepitvat dál `FILMY`.
- Zaměřit se čistě na výměnu nebo opravu `Tailscale` instalace na `mac-mini`.
- Praktický kandidát je přejít z App Store varianty na standalone/macOS variantu, která poskytuje funkční CLI a LocalAPI/socket pro `Caddy`.

## Stav po přechodu na Standalone 2026-07-23

- App Store build už je pryč:

```text
mdls -name kMDItemAppStoreHasReceipt /Applications/Tailscale.app
kMDItemAppStoreHasReceipt = (null)
```

- Bundle identifier se změnil na standalone variantu:

```text
defaults read /Applications/Tailscale.app/Contents/Info CFBundleIdentifier
io.tailscale.ipn.macsys
```

- Přesto `tailscale version`, `tailscale status` i `tailscale ip -4` dál padají na:

```text
Tailscale/BundleIdentifiers.swift:47: Fatal error: The current bundleIdentifier is unknown to the registry
```

- `ps` po restartu ukazuje jen hlavní app proces:

```text
/Applications/Tailscale.app/Contents/MacOS/Tailscale
```

- `ls -l /var/run/tailscaled.socket` stále vrací, že socket neexistuje.

## Nová pracovní interpretace

- Přechod z App Store na standalone proběhl úspěšně.
- Samotná výměna build varianty ale nestačila.
- Další podezřelé body jsou teď:
  - launcher nespouští binárku v CLI režimu
  - nebo standalone system extension / VPN část nebyla po instalaci správně aktivovaná
  - nebo je v aktuální Tailscale verzi regresní bug i ve standalone buildu

## Další live průlom 2026-07-23

- Při vynucení CLI režimu standalone binárka funguje:

```bash
TAILSCALE_BE_CLI=1 /Applications/Tailscale.app/Contents/MacOS/Tailscale version
TAILSCALE_BE_CLI=1 /Applications/Tailscale.app/Contents/MacOS/Tailscale status
```

- `version` vrací korektně `1.98.9`.
- `status` vrací normální výstup, takže samotná standalone instalace je funkční.
- Aktivní je i Tailscale network extension:

```text
systemextensionsctl list | rg -i tailscale
[activated enabled] io.tailscale.ipn.macsys.network-extension
```

## Co to znamená prakticky

- Původní problém s App Store variantou je vyřešený.
- Defaultní `tailscale` launcher na `/usr/local/bin/tailscale` je ale na tomhle stroji pořád rozbitý nebo nevolá binárku správným způsobem.
- To už není hlavní blocker pro `FILMY`.

## Nový důležitý blocker po reinstalaci

- `tailscale status` ukazuje, že aktuální stroj už není původní node `mini`, ale nový node:

```text
100.91.68.48    kulda-mini-3   ...
100.124.124.95  mini           ... offline
```

- Tím pádem:
  - stará Tailscale IP `100.124.124.95` už patří offline zařízení `mini`
  - aktuální `Caddyfile` pořád binduje na starou IP `100.124.124.95`
  - hostname `mini.taildce711.ts.net` velmi pravděpodobně míří na starý/offline node

## Aktuální nejpravděpodobnější vysvětlení

- Dřívější TLS problém byl skutečně rozbitý Tailscale runtime.
- Po reinstalaci ale vznikl nový node, takže teď už nestačí jen opravený runtime.
- Je potřeba srovnat i identitu zařízení:
  - zjistit aktuální MagicDNS jméno nového node
  - upravit `Caddyfile`, aby bindoval na aktuální Tailscale IP
  - testovat proti aktuálnímu hostname nového node, ne proti starému `mini.taildce711.ts.net`, pokud už patří offline stroji

## Potvrzení identity node 2026-07-23

- Aktuální Tailscale IPv4 nového node je:

```text
100.91.68.48
```

- Lokální hostname macOS je:

```text
kulda-mini
```

- MagicDNS záznam nového node je:

```text
kulda-mini-3.taildce711.ts.net -> 100.91.68.48
```

- Původní hostname pořád míří na starý offline node:

```text
mini.taildce711.ts.net -> 100.124.124.95
```

## Praktický závěr

- `mini.taildce711.ts.net` už teď není správný cíl pro testy `FILMY`.
- Aktuální testovací hostname je `kulda-mini-3.taildce711.ts.net`.
- `Caddyfile` je potřeba upravit z:

```caddyfile
bind 100.124.124.95
```

- na:

```caddyfile
bind 100.91.68.48
```

- A následné testy mají běžet proti:

```text
https://kulda-mini-3.taildce711.ts.net:8019
https://kulda-mini-3.taildce711.ts.net:8020
```

## Potvrzený průlom: HTTPS funguje na novém node

Po úpravě `Caddyfile` na nový hostname/IP a restartu `Caddy` už HTTPS handshake funguje správně pro oba porty:

- `https://kulda-mini-3.taildce711.ts.net:8019`
- `https://kulda-mini-3.taildce711.ts.net:8020`

Potvrzené vlastnosti z live `curl -vk` testů 2026-07-23:

- DNS se resolvuje na správnou novou Tailscale IP `100.91.68.48`
- TLS handshake doběhne úspěšně
- používá se `TLSv1.3`
- certifikát je platný a ověřený
- subject certifikátu je `CN=kulda-mini-3.taildce711.ts.net`
- issuer je `Let's Encrypt`
- ALPN přepne na `h2`
- request je skutečně odeslaný přes HTTPS na oba backendy

Praktický závěr:

- původní HTTPS/Tailscale blocker je vyřešený
- root cause byla kombinace:
  - rozbitý App Store Tailscale runtime
  - po reinstalaci změněná node identita / MagicDNS hostname / Tailscale IP

Zbývající drobnost:

- v zachyceném výstupu chybí ještě finální HTTP status řádek po odeslání requestu
- pro úplné uzavření je vhodné ještě jednou ověřit stručně:

```bash
curl -skI https://kulda-mini-3.taildce711.ts.net:8019
curl -skI https://kulda-mini-3.taildce711.ts.net:8020
```

## Finální HTTP ověření 2026-07-23

- `curl -skI https://kulda-mini-3.taildce711.ts.net:8019` vrací:

```text
HTTP/2 405
allow: GET
server: uvicorn
via: 1.1 Caddy
```

- `curl -skI https://kulda-mini-3.taildce711.ts.net:8020` vrací:

```text
HTTP/2 405
allow: GET
server: uvicorn
via: 1.1 Caddy
```

Interpretace:

- `405` je tady v pořádku, protože `curl -I` posílá `HEAD`, zatímco backend povoluje jen `GET`.
- Důležité je, že request prošel přes:
  - DNS
  - Tailscale
  - TLS
  - `Caddy`
  - až do `uvicorn` backendu

## Stav uzavření

- HTTPS přístup k `FILMY` i druhé appce přes nový node `kulda-mini-3.taildce711.ts.net` je funkční.
- Původní problém z 2026-07-22 je vyřešený.

## Finální stabilizace hostname 2026-07-23

- Nový node byl v Tailscale adminu přejmenovaný z `kulda-mini-3` zpět na `kulda-mini`.
- Aktuální MagicDNS jméno je:

```text
kulda-mini.taildce711.ts.net -> 100.91.68.48
```

- Finální HTTPS test pro `FILMY` vrací plný úspěšný response:

```text
curl -vk https://kulda-mini.taildce711.ts.net:8019
HTTP/2 200
server: uvicorn
via: 1.1 Caddy
```

- Stručné `HEAD` ověření vrací u obou app:

```text
curl -skI https://kulda-mini.taildce711.ts.net:8019
HTTP/2 405
allow: GET
via: 1.1 Caddy

curl -skI https://kulda-mini.taildce711.ts.net:8020
HTTP/2 405
allow: GET
via: 1.1 Caddy
```

- `405` je očekávané, protože backendy povolují `GET`, zatímco `curl -I` používá `HEAD`.

## Finální provozní stav

- Produkční / záložkový hostname pro tento stroj je zpět:

```text
kulda-mini.taildce711.ts.net
```

- `FILMY` je dostupné na:

```text
https://kulda-mini.taildce711.ts.net:8019
```

- Druhá appka je dostupná na:

```text
https://kulda-mini.taildce711.ts.net:8020
```

- Tailscale + Caddy HTTPS vrstva na `mac-mini` je tímto opravená a ověřená end-to-end.

## Provozní poznámka k `Caddy`

- Ruční spuštění:

```bash
sudo /opt/homebrew/bin/caddy run --config /Users/kulda/apps/Caddyfile
```

drží proces v popředí a váže ho na otevřené okno Terminálu.

- Pro trvalý provoz bez otevřeného terminálu je připravený `launchd` plist:

```text
deploy/cz.kulda.caddy.plist
```

- Je určený pro instalaci jako `LaunchDaemon` do:

```text
/Library/LaunchDaemons/cz.kulda.caddy.plist
```

- Tím `Caddy` poběží na pozadí i po restartu stroje, bez potřeby držet otevřené shell okno.

## Přesný bezpečný postup: App Store -> Standalone

Tahle sekce je pracovní návod pro `mac-mini` po dnešním ověření.

### Proč právě tohle

- Oficiální dokumentace Tailscale dnes doporučuje na macOS primárně `Standalone` variantu z jejich package serveru, ne Mac App Store variantu.
- Tailscale zároveň výslovně nedoporučuje mít na jednom Macu současně App Store a Standalone variantu.
- Dnešní live stav na `mac-mini` přesně odpovídá tomu, že App Store build je rozbitý pro náš provoz:
  - App Store receipt je přítomný
  - CLI padá
  - chybí použitelný LocalAPI/socket pro `Caddy`

### Fáze 0: před změnou

Nejdřív si jen poznamenej současný stav:

```bash
which tailscale
ls -l /usr/local/bin/tailscale
ps aux | rg '[T]ailscale|[t]ailscaled'
ls -l /var/run/tailscaled.socket
curl -vk https://mini.taildce711.ts.net:8019
```

Očekávaný dnešní stav:
- CLI padá na `BundleIdentifiers.swift:47`
- socket neexistuje
- HTTPS handshake končí `tlsv1 alert internal error`

### Fáze 1: odstranit App Store variantu

1. Ukončit `Tailscale.app`.
2. Smazat `Tailscale.app` z `/Applications`.
3. Vysypat koš.
4. Restartovat `mac-mini`.

Poznámka:
- Tailscale doporučuje při přepínání mezi macOS variantami smazat současnou `Tailscale.app`, vysypat koš a rebootovat před instalací jiné varianty.

### Fáze 2: odstranit starou VPN konfiguraci, pokud zůstane viset

Po restartu zkontrolovat v macOS:

- `System Settings` -> `Network` -> `VPN`
- pokud tam `Tailscale` zůstane, odstranit konfiguraci

Teprve když by po GUI odinstalaci zůstával bordel, použít hlubší cleanup podle Tailscale docs:
- smazání zbytkových souborů v `~/Library/...` a `/Library/Tailscale`
- případně smazání Tailscale položek z Keychain

Tohle je až druhý krok, ne první. Není důvod jít hned s plamenometem do Keychainu.

### Fáze 3: nainstalovat Standalone variantu

1. Stáhnout aktuální macOS `Standalone` variantu z oficiálního Tailscale package serveru.
2. Nainstalovat `.pkg`.
3. Otevřít Tailscale a povolit potřebná systémová oprávnění / system extension, pokud si o ně macOS řekne.
4. Přihlásit zařízení zpět do tailnetu.

### Fáze 4: zapnout CLI integraci

Podle aktuální dokumentace se u Standalone varianty CLI integrace instaluje z UI:

1. Otevřít Tailscale.
2. `Settings`
3. najít `CLI integration`
4. `Show me how`
5. `Install Now`

Výsledek má být funkční launcher v:

```text
/usr/local/bin/tailscale
```

### Fáze 5: první ověření po instalaci

Po přihlášení a CLI integraci ověřit:

```bash
which tailscale
tailscale version
tailscale status
tailscale ip -4
ps aux | rg '[T]ailscale|[t]ailscaled'
ls -l /var/run/tailscaled.socket
```

Co chceme vidět:
- `tailscale version` už nespadne
- `tailscale status` vrátí normální stav zařízení
- `tailscale ip -4` vrátí Tailscale IP
- ideálně se objeví použitelný socket nebo jinak funkční LocalAPI vrstva pro `.ts.net`

### Fáze 6: znovu ověřit Caddy + HTTPS

Pak hned pustit:

```bash
/opt/homebrew/bin/caddy validate --config /Users/kulda/apps/Caddyfile
curl -vk http://mini.taildce711.ts.net:8019
curl -vk https://mini.taildce711.ts.net:8019
curl -vk https://mini.taildce711.ts.net:8020
```

Interpretace:
- `http://mini.taildce711.ts.net:8019` má dál vracet chybu typu plain HTTP na HTTPS listener, to je v pořádku
- rozhodující je, aby `https://mini.taildce711.ts.net:8019` a `:8020` přestaly padat na TLS internal error

### Fáze 7: teprve potom řešit Caddy restart, pokud bude potřeba

Když po výměně Tailscale nebude HTTPS fungovat hned, teprve pak:

```bash
ps aux | rg '[c]addy'
sudo pkill caddy
sudo /opt/homebrew/bin/caddy run --config /Users/kulda/apps/Caddyfile
```

Tohle dělat až po opravě Tailscale vrstvy. Dnes už víme, že samotné přepisování `Caddyfile` problém neřeší.

## Praktická pracovní poznámka

- Z oficiální dokumentace vyplývá doporučení pro `Standalone` variantu; konkrétní závěr, že právě ta má opravit náš dnešní problém s `.ts.net` HTTPS, je moje inference z dnešního live stavu:
  - App Store build je potvrzený
  - CLI je rozbitý
  - LocalAPI/socket chybí
  - `Caddy` a backendy jinak žijí

## Zdroje continuity

- `PLAN.md` checkpoint z `2026-07-22`
- `Historie projektu.md` sekce `2026-07-22 - Oprava deploy cesty pro Mac mini LaunchAgent`
- `work/checkpoints/2026-07-22_21-37_checkpoint-quick.md`

## Stručné shrnutí pro další použití

Tento soubor už neslouží jen pro `FILMY`, ale i jako referenční záznam pro
stejný `mac-mini`, `Tailscale` a `Caddy` setup v projektu `AI/apps2`.

Co se skutečně pokazilo:

- App Store varianta `Tailscale.app` na `mac-mini` byla rozbitá pro CLI i `.ts.net` HTTPS provoz.
- `Caddy` proto neuměl korektně obsloužit TLS pro `*.taildce711.ts.net`.
- Po přechodu na standalone `Tailscale` se navíc změnila identita node, takže bylo potřeba srovnat:
  - hostname
  - Tailscale IP
  - `Caddyfile`
- Později se ukázalo, že `Caddy` už běží správně, ale log dál špinil starý duplicitní `LaunchAgent`; finální čisté řešení je nechat `Caddy` jen pod systémovým `LaunchDaemon`.

Co je finální správný stav:

- aktivní node: `kulda-mini`
- MagicDNS hostname: `kulda-mini.taildce711.ts.net`
- Tailscale IP: `100.91.68.48`
- `FILMY`: `https://kulda-mini.taildce711.ts.net:8019`
- druhá appka: `https://kulda-mini.taildce711.ts.net:8020`
- `FILMY` app běží pod `LaunchAgent` `cz.kulda.filmy`
- `Caddy` běží pod `LaunchDaemon` `cz.kulda.caddy`

## Finální ověřovací příkazy

### Na `mac-mini`

Ověření Tailscale node:

```bash
TAILSCALE_BE_CLI=1 /Applications/Tailscale.app/Contents/MacOS/Tailscale status
TAILSCALE_BE_CLI=1 /Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4
host kulda-mini.taildce711.ts.net
```

Ověření `Caddy` a HTTPS:

```bash
sudo launchctl print system/cz.kulda.caddy
curl -skI https://kulda-mini.taildce711.ts.net:8019
curl -skI https://kulda-mini.taildce711.ts.net:8020
```

Krátká interpretace:

- `host` má vrátit `100.91.68.48`
- `curl -skI` má vracet `HTTP/2 405`, `allow: GET`, `server: uvicorn`, `via: 1.1 Caddy`
- `405` je zde v pořádku, protože `curl -I` dělá `HEAD`, zatímco backendy povolují `GET`

Ověření, že `Caddy` není vázané na ruční shell:

```bash
ps aux | rg '[c]addy'
sudo launchctl list | rg caddy
```

Očekávaný stav:

- jedna běžící instance `caddy`
- jeden `launchd` job `cz.kulda.caddy`

### Na klientském MacBooku

Pokud URL nefungují mimo `mac-mini`, první kontrola je vždy klientský Tailscale stav:

```bash
TAILSCALE_BE_CLI=1 /Applications/Tailscale.app/Contents/MacOS/Tailscale status
host kulda-mini.taildce711.ts.net
curl -skI https://kulda-mini.taildce711.ts.net:8019
```

Pokud `host` vrací `Could not resolve host`, problém je na klientovi, ne na serveru.

## Závěr

K 2026-07-23 je Tailscale + Caddy vrstva na `mac-mini` opravená a ověřená
end-to-end. Kritické poučení pro příště:

- na tomhle stroji nepoužívat App Store variantu Tailscale pro podobný provoz
- po reinstalaci Tailscale vždy znovu ověřit hostname a IP node
- `Caddy` provozovat přes systémový `LaunchDaemon`, ne ručně v Terminálu
- když URL nefunguje z jiného zařízení, nejdřív ověřit klientský Tailscale stav, ne hned znovu rozbírat server

## Follow-up: FILMY deploy a TMDB badge

Při dotažení samotného deploye `FILMY` na `mac-mini` se ukázaly ještě tři
praktické provozní opravy mimo Tailscale:

- startup guard pro PostgreSQL katalog nesměl na serveru s hotovým PG katalogem
  zbytečně vyžadovat lokální `imdb/*.tsv`
- TMDB asset existence nesměla po přesunu mezi stroji spoléhat jen na staré
  absolutní `local_path`, ale musela umět dopočítat aktuální cestu i z
  `relative_path`
- serverový DB upgrade runner nesměl při každém upgradu znovu přehrávat
  `001_bootstrap.sql` nad už existující databází

Další poznatek z live nasazení:

- na tomto stroji je ověřený funkční upgrade příkaz
  `.venv/bin/python -m filmy.scripts.upgrade_database`
- varianta `uv run filmy-upgrade-database` tu zatím není spolehlivá, protože
  `uv sync` neinstaluje `project.scripts` entrypointy

Finální výsledek:

- po `git pull`, `uv sync`, `.venv/bin/python -m filmy.scripts.upgrade_database`
  a restartu `cz.kulda.filmy` se na `mac-mini` vše zobrazuje normálně
- badge `TMDB fetching in background`, který se ukazoval i po přenosu
  `data/assets`, zmizel
