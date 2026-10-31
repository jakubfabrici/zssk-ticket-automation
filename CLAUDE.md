# ZSSK ticket automation: ako s tým pracovať z cloudovej session

Odpovedaj po slovensky, tykaj. Bez pomlčiek typu em dash v textoch a v UI.

## Kde čo beží

Aplikácia beží doma na Proxmoxe, nie v cloude. Táto cloudová session ju
riadi cez homelab MCP server (nástroje `proxmox_guest_exec`, `ssh_exec`).

| čo | kde |
| --- | --- |
| produkcia | LXC 121 `zssk`, 192.168.1.251, Debian 12, 1 GB RAM, nesting=1 |
| aplikácia | `/opt/zssk` (zssk_bulk.py, zssk_web.py, zssk_runner.py, zssk_users.py, templates/, static/, .venv) |
| kópia kitu | `/opt/zssk-kit` (odtiaľ sa spúšťa `install.sh`; mock e-shopu v `mock/`) |
| konfigurácia | `/etc/zssk/users.json` a `/etc/zssk/users/<uid>/{routes.json,person.txt,credentials.env}` (0600/0700) |
| stav | `/var/lib/zssk/users/<uid>/{run.lock,debug/,downloads/,profile/}` |
| služby | `zssk-web.service` (Flask na :8080), `zssk-topup.timer` (vypnutý, nechať vypnutý) |
| verejný názov | https://zssk.fabrici.xyz, len LAN a VPN, inak deny |
| Proxmox host | `pve`, 192.168.1.200 (`pct exec 121 -- ...`, `pct push 121 ...`) |

Zdroj pravdy pre kód je tento repozitár na GitHube (vetva `main`). Starý
vývojový strom `~/zssk-dev` na CT106 a worktree `/root/claudecode-zssk` na
pve sú historické kópie, do repozitára sa zlúčili 2. 10. 2026.

## Nasadenie zmeny

1. Zmeň kód tu, spusti testy: `python -m pytest -q` vo venv s Flask,
   Selenium a pytest (129 testov, browserové sa preskočia bez Firefoxu).
2. Commit a push do pracovnej vetvy, PR do `main`.
3. Nasadenie kódu (bez reinštalácie, zachová `/etc/zssk` aj `/var/lib/zssk`):
   v LXC 121 naklonuj vetvu do `/opt/zssk-src`, zálohuj súbory z
   `/opt/zssk` do `/opt/zssk-backup-<čas>`, skopíruj `zssk_*.py`,
   `templates/*.html` a `static/style.css` a reštartuj `zssk-web`.
   Predtým over, že nebeží nákup. Pozor: `pgrep -f zssk_bulk` chytí aj
   vlastný `sh -lc` príkaz, preto kontroluj ukotvene:

```
pgrep -x Xvfb; pgrep -x firefox-esr; pgrep -x geckodriver
pgrep -f '^(/usr/bin/xvfb-run|/opt/zssk/.venv/bin/python /opt/zssk/zssk_bulk)'
```

   Plná reinštalácia cez `install.sh` (z `/opt/zssk-kit`) je potrebná len
   pri zmene závislostí, systemd jednotiek alebo adresárovej štruktúry.

Overenie nasadenia: porovnaj `md5sum` súborov v `/opt/zssk` s repozitárom.

## Pevné pravidlá (Jakubove pokyny)

- Nikdy nekupuj lístky za neho: žiadne `--confirm`, žiadne klikanie na
  „Kúpiť“, timer ostáva vypnutý. Lístky si kupuje sám vo webovom UI.
  Smieš len opravovať a diagnostikovať. Skúšobný beh (`--dry-run`) proti
  živému e-shopu len na jeho výslovnú žiadosť a striedmo.
- Heslo do ZSSK konta nikdy nepýtaj a nevkladaj do chatu. Zadáva ho sám
  v Nastaveniach UI, ukladá sa do `credentials.env` (0600).
- Osobné údaje (meno, e-mail, číslo registrácie, číslo konta) nepatria do
  repozitára, do commitov ani do PR. Repozitár je verejný.
- Pri vývoji nikdy nespúšťaj `zssk_bulk.py` proti živej stránke; použi
  mock v `mock/` (testy ho obsluhujú samy).
- Behy proti živej stránke dávkuj: 45 s medzi nákupmi, pár behov denne.
  F5/Shape bot ochrana už raz na ~20 minút zablokovala domácu verejnú IP
  (TLS reset, HTTP 000). Firefox cez `xvfb-run`, nie headless.
- Bez dohody nemerguj a nepushuj do `main`.

## Čo o e-shope platí (overené na živej stránke, september 2026)

- Aplikácia je na `https://predaj.zssk.sk/` (JSF/PrimeFaces, F5 bot
  ochrana). Prihlásenie je samostatná SPA na `/aut/` (`data-cy=login-email`,
  `data-cy=login-pwd`).
- Tok: `/search` → výsledky (`eSalesConnectionLoop`, „Odrieknutý vlak“ pri
  zrušených) → `[data-cy=buy-ticket]` → `/passenger` (veková kategória
  `data-age-category-id=204` Mladý 18 až 25, zľava `data-discount-type-id`
  617 ISIC/BČK aktivované školou SR, `freeTicket` je predvolene zaškrtnutý)
  → 4× `[data-cy=continue]` → `/personal` (autocomplete uloženého
  cestujúceho z konta) → `/payment` → `/summary` (Košík 0,00 €) → Zaplatiť.
- „Na zvolený čas cesty už existuje vydaný bezplatný lístok“ blokuje celý
  deň, nie len spoj (stav `exists`).
- Pamäť kúpených lístkov (ledger) bola 26. 9. zámerne zrušená: každý beh
  skúša všetky dni, duplikáty odmietne ZSSK.
- Lístky kúpené cez web sa v apke Ideme vlakom zobrazujú bez riadku ISIC.
  PDF v maili je úplné. Nie je to naša chyba.

## Otvorené veci (stav k 2. 10. 2026)

- Behy končili „Nepodarilo sa“, lebo vlak 13:07 Ex 618 Košice → SNV na
  štvrtky v októbri reálne nejde (náhradná doprava). Dve trasy na 13:07 sú
  duplicitné. Možnosti: zmeniť trasu na idúci spoj, vypnúť duplikát, alebo
  v kóde spraviť z „spoj v ten deň nejde“ pokojný stav, nie chybu celého
  behu.
- README ešte opisuje ledger a jednopoužívateľský režim, treba ho
  zaktualizovať.
- 31. 10. 2026 nasadená vetva `claude/sharp-lamport-jg5pyu` (commit e89642e),
  záloha predchádzajúcej verzie v `/opt/zssk-backup-20261031-110458`.
- zssk.fabrici.xyz má v Caddy `import lan_only` (len 192.168.1.0/24, bez
  VPN). Zmena na `import gate` je Jakubovo rozhodnutie.
