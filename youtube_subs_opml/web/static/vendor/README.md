# Vendored front-end assets

Served from `/static/vendor` so the app has no runtime dependency on a CDN
(a self-hosted instance keeps working offline or behind a strict firewall).

| File | Source | Licence |
| --- | --- | --- |
| `htmx-2.0.4.min.js` | npm `htmx.org@2.0.4`, `dist/htmx.min.js` | 0BSD (`htmx-LICENSE.txt`) |
| `sortable-1.15.6.min.js` | npm `sortablejs@1.15.6`, `Sortable.min.js` | MIT (`sortable-LICENSE.txt`) |
| `archivo-*.woff2`, `archivo.css` | Google Fonts, Archivo v25 (variable weight) | SIL OFL 1.1 (`archivo-OFL.txt`) |

To upgrade one, replace the file with the new version (keep the version in
the file name) and update the `<script>` tag that loads it.
