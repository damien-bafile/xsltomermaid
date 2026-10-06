# Vendored third-party assets

## mermaid.min.js

- **Library:** [Mermaid](https://github.com/mermaid-js/mermaid)
- **Version:** 10.9.1
- **License:** MIT (© Knut Sveidqvist and Mermaid contributors)

Bundled locally so the in-app "Rendered diagram" view works offline (no CDN
required). Loaded by `diagram_view.py` into a `QWebEngineView`.

To update, replace the file with a newer UMD build, e.g. via npm:

```bash
npm pack mermaid@<version>
tar xzf mermaid-<version>.tgz
cp package/dist/mermaid.min.js src/xsltomermaid/vendor/mermaid.min.js
```
