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
  way through `ikvc.slovakrail.sk/esales` using absolute XPaths and generated
  JSF ids (`j_idt261`, …). Those break whenever ZSSK reskins the e-shop, and
  ZSSK has reworked its sales channels since. The patch in `patches/` fixes the
  *runtime* (Selenium 4, Python 3.12+), **not** the selectors — expect to
  re-map them. Always do a `--stop-before-pay` run first and look at the
  screenshot it drops in `/opt/zssk/debug-*`.
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

## What the patch changes

`patches/0001-modernize-selenium4-python3.patch`, against upstream `a6bcffd`:

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
| `NoSuchElementException` on a long XPath | ZSSK changed the page; re-map that selector against the dumped `debug-*.html` |
| Firefox fails to start in the LXC | container needs `--features nesting=1` (bootstrap sets it) |
| `geckodriver` not found | rerun `install.sh`, or set `GECKODRIVER_VERSION` |
| the run stops on personal data | `person.txt` fields are empty or the registration number is wrong |

## Security

`person.txt` holds personal data: it lives only inside the container, at mode
`0600`, and `deploy/zssk/person.txt` is git-ignored so a local copy cannot be
committed by accident. The container needs nothing but outbound HTTPS — do not
expose it, and do not give it the MCP bearer token.
