# Instalace projektu na jinem pocitaci

Kratky postup pro novy stroj nebo druhou kopii projektu.

## Predpoklady

- macOS nebo Linux
- Python 3.14+
- `uv`
- Git
- PostgreSQL, pokud ma bezet aktualni PG runtime s realnymi daty

## Prvni instalace

```bash
git clone https://github.com/kulda69/FILMY.git
cd FILMY
uv sync
```

Pokud uz je repozitar na stroji stazeny:

```bash
cd /cesta/k/FILMY
git pull --ff-only
uv sync
uv run filmy-upgrade-database
```

## Lokalni konfigurace

V rootu projektu vytvor `.env`. Hesla a tokeny nepatri do gitu.

Minimalni PostgreSQL runtime potrebuje hlavne:

```text
POSTGRES_APP_HOST=/private/tmp
POSTGRES_APP_PORT=5432
POSTGRES_APP_DATABASE=filmy
POSTGRES_APP_USER=filmy_app
POSTGRES_APP_PASSWORD=...
```

Volitelne pro TMDB enrichment:

```text
TMDB_API_READ_ACCESS_TOKEN=...
```

Administratorske PostgreSQL promenne jsou potreba jen pro bootstrap, migrace
nebo obnovu databaze; bezne spusteni appky je nema potrebovat.

Po nasazeni na server se databaze uz nema rucne prenaset z vyvojoveho stroje.
Po kazdem `git pull` spust databazovy upgrade:

```bash
uv run filmy-upgrade-database
```

Runner si v PostgreSQL vede tabulku `app.database_upgrades`, takze opakovane
spusteni ma byt bezpecne a provede jen chybejici verziovane kroky.

## Data

Git obsahuje kod a dokumentaci, ne lokalni databaze, assety ani exporty. Na
novem pocitaci je potreba dodat data jednou z cest:

- obnovit PostgreSQL databazi `filmy` ze zalohy,
- nebo znovu spustit importni/bootstrap postupy,
- a pripadne prenest lokalni assety z `data/assets/`, pokud jsou potreba hned.

Poznamka k `imdb/`:

- pokud na novem stroji obnovis uz hotovou PostgreSQL databazi vcetne
  katalogovych tabulek `app.catalog_*`, aplikace uz pro bezny start nepotrebuje
  lokalni `imdb/*.tsv` soubory;
- adresar `imdb/` s rozbalenymi TSV je potreba jen pro prvotni build/rebuild
  katalogu, `System -> IMDb Refresh` nebo vedomy servisni rebuild z aktualnich
  dumpu.

## Kontrola a spusteni

```bash
.venv/bin/pytest
.venv/bin/python main.py
```

Aplikace bezi na:

```text
http://127.0.0.1:8019
```

## Automaticke spusteni na Mac mini

V repozitari je pripraveny LaunchAgent:

```text
deploy/cz.kulda.filmy.plist
```

Je nastaveny na cestu:

```text
/Users/kulda/apps/FILMY
```

Pokud bude projekt na Mac mini jinde, uprav v plist hodnoty
`ProgramArguments` a `WorkingDirectory`.

Instalace LaunchAgentu:

```bash
mkdir -p logs
cp deploy/cz.kulda.filmy.plist ~/Library/LaunchAgents/cz.kulda.filmy.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/cz.kulda.filmy.plist
launchctl enable gui/$(id -u)/cz.kulda.filmy
launchctl kickstart -k gui/$(id -u)/cz.kulda.filmy
```

Kontrola:

```bash
launchctl print gui/$(id -u)/cz.kulda.filmy
tail -n 100 logs/launchd.stderr.log
```

Odinstalace:

```bash
launchctl bootout gui/$(id -u)/cz.kulda.filmy
rm ~/Library/LaunchAgents/cz.kulda.filmy.plist
```

## Automaticke spusteni Caddy na Mac mini

Pro `Caddy` je v repozitari pripraveny samostatny `launchd` plist:

```text
deploy/cz.kulda.caddy.plist
```

Je nastaveny na:

- binarku `/opt/homebrew/bin/caddy`
- konfiguraci `/Users/kulda/apps/Caddyfile`
- `HOME=/var/root`, aby `Caddy` pod `LaunchDaemon` nespadl do prazdneho `$HOME`
- logy `/Users/kulda/apps/logs/caddy.stdout.log` a `caddy.stderr.log`

Na rozdil od `FILMY` appky je `Caddy` prakticke spoustet jako `LaunchDaemon`,
aby nebyl navazany na otevreny terminal ani na prihlasene GUI sezeni.

Instalace `LaunchDaemon`:

```bash
sudo mkdir -p /Users/kulda/apps/logs
sudo cp deploy/cz.kulda.caddy.plist /Library/LaunchDaemons/cz.kulda.caddy.plist
sudo chown root:wheel /Library/LaunchDaemons/cz.kulda.caddy.plist
sudo chmod 644 /Library/LaunchDaemons/cz.kulda.caddy.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/cz.kulda.caddy.plist
sudo launchctl enable system/cz.kulda.caddy
sudo launchctl kickstart -k system/cz.kulda.caddy
```

Kontrola:

```bash
sudo launchctl print system/cz.kulda.caddy
tail -n 100 /Users/kulda/apps/logs/caddy.stderr.log
tail -n 100 /Users/kulda/apps/logs/caddy.stdout.log
```

Reload po zmene `Caddyfile`:

```bash
/opt/homebrew/bin/caddy validate --config /Users/kulda/apps/Caddyfile
sudo launchctl kickstart -k system/cz.kulda.caddy
```

Odinstalace:

```bash
sudo launchctl bootout system /Library/LaunchDaemons/cz.kulda.caddy.plist
sudo rm /Library/LaunchDaemons/cz.kulda.caddy.plist
```

## Rychla aktualizace pozdeji

```bash
cd /cesta/k/FILMY
git pull --ff-only
uv sync
uv run filmy-upgrade-database
.venv/bin/pytest
.venv/bin/python main.py
```
