# Docs Deployment

This framework retains the English VitePress documentation and its local build configuration. The upstream publishing workflow described below is historical context; this source bundle does not include a GitHub Actions deployment workflow. Configure hosting separately when publishing your own copy.

## Local preview

```bash
npm ci
npm run docs:dev
```

Build static files:

```bash
npm run docs:build
```

The build output is:

```text
docs/.vitepress/dist
```

## GitHub Pages settings

For first-time setup, open:

`Settings -> Pages -> Build and deployment -> Source -> GitHub Actions`

Automatic deployment requires adding and configuring a deployment workflow in your own repository.

## Release and documentation updates

The upstream release process keeps `metadata.yaml`, the registration version in `main.py`, the backup manager's `PLUGIN_VERSION`, `package.json` and the lockfile in sync. It adds a matching `CHANGELOG.md` section with PR links and authors and updates [Releases and Upgrades](/en/releases). Framework releases instead use the root `tools/build_release.py` script and the instructions in the root README.

In the upstream repository, `Auto Release on Version Update` tags the workflow-triggering commit, marks beta/alpha/rc/pre versions as prereleases, and `Deploy VitePress Docs` publishes the site separately. These workflows are not part of this framework source tree.

## URLs

Default Pages URL:

```text
https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/
```

English docs:

```text
https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/en/
```
