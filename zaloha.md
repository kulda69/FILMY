# Zaloha PostgreSQL databaze `filmy`

## Hotovy prikaz

```bash
PGPASSWORD='heslo' /Library/PostgreSQL/14/bin/pg_dump -h /private/tmp -p 5432 -U postgres -d filmy -F c -b -v -f filmy.dump
```

## Co znamena

- `PGPASSWORD='heslo'`
  Heslo k PostgreSQL uctu pro toto jedno spusteni prikazu.

- `/Library/PostgreSQL/14/bin/pg_dump`
  Plna cesta k programu `pg_dump` na tomhle Macu.

- `-h /private/tmp`
  Pripojeni pres Unix socket adresar `/private/tmp`.

- `-p 5432`
  PostgreSQL port.

- `-U postgres`
  Uzivatel, pod kterym se dump spousti.

- `-d filmy`
  Nazev zalohovane databaze.

- `-F c`
  `custom` format dumpu, vhodny pro pozdejsi obnovu pres `pg_restore`.

- `-b`
  Zahrne i `large objects`, pokud by v databazi byly.

- `-v`
  Verbose vystup, aby byl videt prubeh.

- `-f filmy.dump`
  Vystupni soubor zalohy v aktualnim adresari.

## Varianta s plnou cestou k vystupnimu souboru

```bash
PGPASSWORD='heslo' /Library/PostgreSQL/14/bin/pg_dump -h /private/tmp -p 5432 -U postgres -d filmy -F c -b -v -f /Volumes/not_inserted/PycharmProjects/FILMY/backups/filmy.dump
```

Poznamka:
Adresar `backups` musi existovat predem.
