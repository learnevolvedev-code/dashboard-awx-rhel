# frontend/vendor/

This folder holds **local copies of all frontend JavaScript libraries and fonts**
used by `index.html`. When these files are present the portal runs completely
offline — no internet access required at runtime.

## Contents (after running the download script)

```
vendor/
├── react.production.min.js       React 18.3.1
├── react-dom.production.min.js   ReactDOM 18.3.1
├── Recharts.js                   Recharts 2.12.7
├── babel.min.js                  Babel Standalone 7.24.7
└── fonts/
    ├── ibm-plex.css              Combined @font-face CSS
    ├── ibm-plex-sans/            IBM Plex Sans WOFF2 files (weights 300–600)
    └── ibm-plex-mono/            IBM Plex Mono WOFF2 files (weights 400–500)
```

## How to populate this folder

From the repository root:

```bash
bash scripts/download-vendor.sh
```

Then copy to the server install location:

```bash
cp -r frontend/vendor /opt/awx-portal/frontend/
```

Or let `install.sh` do it automatically (it calls `download-vendor.sh` if
the vendor/ folder is empty).

## How index.html decides which to use

On page load a synchronous `HEAD` request checks whether
`vendor/react.production.min.js` is reachable:

- **200 OK** → all four libraries load from `vendor/`
- **404 / network error** → falls back to CDN (unpkg.com)

Fonts always attempt `vendor/fonts/ibm-plex.css` first; the Google Fonts
CDN is loaded simultaneously as a fallback.

## Why these files are NOT committed to git

Binary and minified files bloat git history and make diffs unreadable.
The download script is deterministic — it always produces the same files
from the same pinned versions — so committing the output adds no value.

The `.gitignore` excludes `frontend/vendor/` but keeps this `README.md`.

## Pinned versions

| Library | Version | Source |
|---|---|---|
| React | 18.3.1 | `registry.npmjs.org/react/-/react-18.3.1.tgz` |
| ReactDOM | 18.3.1 | `registry.npmjs.org/react-dom/-/react-dom-18.3.1.tgz` |
| Recharts | 2.12.7 | `registry.npmjs.org/recharts/-/recharts-2.12.7.tgz` |
| Babel Standalone | 7.24.7 | `registry.npmjs.org/@babel/standalone/-/standalone-7.24.7.tgz` |
| IBM Plex Sans | — | Google Fonts CDN |
| IBM Plex Mono | — | Google Fonts CDN |

To upgrade a library: update the version in `scripts/download-vendor.sh`
and re-run it.
