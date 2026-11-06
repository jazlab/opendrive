# Project website

The project website is in `site/index.html`; no build tool is required.

## Preview locally

From the repository root, run:

```bash
python -m http.server 8000 --bind 127.0.0.1 --directory site
```

Open `http://127.0.0.1:8000`. Google Fonts requires an internet connection; system
font fallbacks work offline.

## Publish with GitHub Pages

The website URL is `https://jazlab.github.io/opendrive/`. Publishing uses the
**Publish project website** GitHub Actions workflow and is started manually;
pushing or merging changes does not deploy the site automatically.

1. Update website content and resource links in `site/index.html`, then merge
   the changes into `main`.
2. In Settings → Pages, select **GitHub Actions** as the publishing source.
3. In Settings → Secrets and variables → Actions → Variables, set `PUBLISH_SITE`
   to `true`.
4. In Actions, run **Publish project website** from `main`.

The workflow requires both `PUBLISH_SITE=true` and the `main` branch. Setting
`PUBLISH_SITE` to `false` prevents future deployments; it does not remove an
existing website. To take a published site offline, use Settings → Pages to
unpublish it.
