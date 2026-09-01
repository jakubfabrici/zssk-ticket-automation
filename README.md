# ZSSK ticket automation on Proxmox

Deploys [`europ/zssk-ticket-automation`](https://github.com/europ/zssk-ticket-automation)
— a Selenium script that fills in the ZSSK e-sales checkout for a **free
(100 % discount) ticket** — into a dedicated LXC container, with a wrapper
preset for the **Spišská Nová Ves → Košice** route.

```
pve  ──►  LXC 121 "zssk" (Debian, 1 GB, nesting=1)
             ├── /opt/zssk           upstream checkout + compatibility patch
             ├── /opt/zssk/person.txt passenger data (0600, never in git)
             └── /opt/zssk/buy.sh    wrapper: route defaults + headless Firefox
```

## Read this before you rely on it

* **Upstream is from July 2019 and has not been touched since.** It clicks its
  way through the e-shop using absolute XPaths and generated JSF ids
  (`j_idt261`, …), which break whenever ZSSK redeploys. The patch in
  `patches/` fixes the runtime *and* everything on the search page that was
  checked against the live site (see below) — but the steps after the search
  are still upstream's 2019 ids and are **unverified**. Always do a
  `--stop-before-pay` run first and look at the screenshot it drops in
  `/opt/zssk/debug-*`.
* **The site sits behind F5 Shape bot defense** (the `/TSPD/…` script on every
  page). Expect headless Firefox from a datacenter IP to be reset outright; a
  normal-looking run from the LAN (`-F`, non-headless) has a much better
  chance. Automated checkout is also not something ZSSK's terms invite, so
  keep it to your own free ticket and don't hammer it.
* **For regional trains you may not need a ticket at all.** Since 1 August,
  passengers with a 100 % discount no longer need a zero ticket on **Os and REX**
  trains — a valid document with the discount activated, shown to the
  conductor, is enough. The zero ticket is still required on **long-distance
  trains (R / IC / EC)**. Spišská Nová Ves → Košice is served by both, so check
  which train you are taking before automating anything.
* **The script never logs in and never pays.** It is the guest checkout for a
  0 € ticket, keyed by the registration number in `person.txt`. Do not point it
  at a paid fare.
* A wrong ticket can be cancelled at `ikvc.slovakrail.sk/esales/refund`.

## Install

From a checkout of this repo **on the Proxmox host**, as root:

```bash
bash deploy/zssk/bootstrap.sh
```

Tunables (all optional, defaults in the script): `VMID=121`,
`IP_CIDR=192.168.1.251/24`, `BRIDGE`, `GATEWAY`, `STORAGE`, `DISK_GB=6`,
`MEM_MB=1024`, `CORES=2`.

The bootstrap creates the container, pushes this directory into it and runs
`install.sh` inside, which installs `firefox-esr` + `geckodriver`, checks out
upstream at the pinned commit `a6bcffd`, applies the compatibility patch and
builds a virtualenv. Both scripts are idempotent — re-running reuses the
container and leaves `person.txt` alone.

To install into an existing container or a plain Debian box instead, copy this
directory over and run `bash install.sh` in it.

## Use

```bash
pct exec 121 -- nano /opt/zssk/person.txt          # name, surname, email, registration number
pct exec 121 -- /opt/zssk/buy.sh --stop-before-pay # dry run, stops at "Súhrn (4)"
pct exec 121 -- /opt/zssk/buy.sh                   # confirms the order
```

`buy.sh` defaults to Spišská Nová Ves → Košice, today, at the current time, in
headless Firefox. Override per run:

```bash
pct exec 121 -- /opt/zssk/buy.sh -d 05.09.2026 -t 07:12
pct exec 121 -- env DEPARTURE="Košice" ARRIVAL="Spišská Nová Ves" /opt/zssk/buy.sh
```

Any flag you pass wins over the default; everything is forwarded to `buy.py`
(`--hold N` keeps the browser open longer, `-F` runs it non-headless).

## What was checked against the live site (1 Sep 2026)

* `ikvc.slovakrail.sk` **no longer serves the e-sales app** — it resolves to
  the same address as `www.zssk.sk` and presents a certificate for
  `CN=www.zssk.sk` that does not cover it, so upstream's URL fails TLS before
  it loads anything. The application moved to **`https://predaj.zssk.sk/`**
  (`/search`, `/refund`); it is the same JSF app, still serving
  `ikvc-scripts.js`.
* All six search-page ids upstream depends on are still there:
  `searchPanel`, `fromInput`, `toInput`, `departDate`, `departTime`,
  `actionSearchConnectionButton` — plus stable `data-cy` attributes
  (`stationFrom`, `stationTo`, `search`) that are better anchors if these ever
  move.
* The page title is now `Vyhľadanie spojenia`, so upstream's
  `assert 'ZSSK' in driver.title` fails immediately.
* `departDate` is `readonly` (a bootstrap datepicker) and renders as
  `1. 9. 2026`, so upstream's `send_keys("18.03.2019")` cannot fill it.
* The station fields are `bootstrap3-typeahead` inputs: a typed value only
  counts once a suggestion from the dropdown is picked.
* Everything past the search button could not be exercised from here — the bot
  defense resets browser connections from this network — so the passenger,
  cart, personal-data and payment steps still carry upstream's 2019 ids.

## What the patch changes

`patches/0001-modernize-selenium4-python3.patch`, against upstream `a6bcffd`:

* base URL → `https://predaj.zssk.sk/search`, overridable with `--url`
* `assert 'ZSSK' in driver.title` → a warning against the real title
* station fields filled through the typeahead dropdown instead of an absolute
  XPath into the old DOM
* the readonly date field set via JS in the `D. M. YYYY` format the form uses

* `find_element_by_id/_by_xpath` → `find_element(By.ID/By.XPATH, …)` (removed in Selenium 4)
* `FirefoxProfile` + `options.headless` → `options.set_preference` / `-headless`
* `imp.load_source` → a literal parser for `person.txt` (`imp` is gone in
  Python 3.12, and the file is no longer executed as code)
* new `--person FILE`, `--stop-before-pay`, `--hold N`
* screenshot + page-source dump on failure and on the dry-run stop, so a stale
  selector is diagnosable instead of just a stack trace

## Troubleshooting

| symptom | cause |
| --- | --- |
| `NoSuchElementException` on a long XPath | one of the unverified post-search steps; re-map it against the dumped `debug-*.html` |
| connection reset / blank page | bot defense; try `-F` (non-headless) and a non-datacenter IP |
| Firefox fails to start in the LXC | container needs `--features nesting=1` (bootstrap sets it) |
| `geckodriver` not found | rerun `install.sh`, or set `GECKODRIVER_VERSION` |
| the run stops on personal data | `person.txt` fields are empty or the registration number is wrong |

## Security

`person.txt` holds personal data: it lives only inside the container, at mode
`0600`, and `deploy/zssk/person.txt` is git-ignored so a local copy cannot be
committed by accident. The container needs nothing but outbound HTTPS — do not
expose it, and do not give it the MCP bearer token.
