# Project website

The supplied project website is in `site/index.html`. Its image is embedded in the
HTML; no build tool is required. The Paper reproducibility link points to
`https://github.com/jazlab/monkeydrive_paper`.

## Preview locally

From the repository root, run:

```bash
python -m http.server 8000 --bind 127.0.0.1 --directory site
```

Open `http://127.0.0.1:8000`. Google Fonts requires an internet connection; system
font fallbacks work offline.

## Publishing is disabled

During release preparation, GitHub rejected enabling Pages because the current
organization plan does not support Pages for this private repository. The source
and workflow are ready, but hosting is not enabled. A supported plan or a
separately approved change to repository visibility is needed before enabling it.

The Pages workflow has **no push trigger**. It only accepts a manual dispatch and
requires the repository variable `PUBLISH_SITE` to equal `true`. Keep that variable
`false` during release preparation. A private source repository does not by itself
make a published Pages website private.

GitHub Pages should use **GitHub Actions** as its publishing source, without a
branch publishing source. The expected URL is `https://jazlab.github.io/MonkeyDrive/`.
The existing repository is named `MonkeyDrive`; capitalization is retained for the
Pages path.

When the project is ready for a public website:

1. Replace the two `href="#"` paper-link placeholders with the published preprint URL.
2. Make sure linked resources are accessible to the intended audience. The paper
   repository remains private until its owner explicitly releases it.
3. Review and merge the release branch into `main` so GitHub can discover the manual
   workflow. No pull request or merge is performed as part of preparing this branch.
4. In Settings → Pages, select **GitHub Actions** if it is not already selected.
5. Set Settings → Secrets and variables → Actions → Variables → `PUBLISH_SITE` to
   `true` only when public publication is intended.
6. Run **Publish project website** manually from `main` in the Actions tab.

Setting the variable back to `false` prevents future deployments; it does not
unpublish a website that has already been deployed. Use Settings → Pages to
unpublish an existing site if needed.
