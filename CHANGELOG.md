# Changelog

## [0.8.0](https://github.com/Zaimwa9/themis/compare/v0.7.1...v0.8.0) (2026-07-15)


### Features

* baseline big-picture architecture pass with tri-state big_picture note ([#60](https://github.com/Zaimwa9/themis/issues/60)) ([3e8e6fe](https://github.com/Zaimwa9/themis/commit/3e8e6fe31d1face363dceefd94df68bac0b4e692))


### Bug Fixes

* **prompts:** oblige resolving own verified-fixed threads ([#64](https://github.com/Zaimwa9/themis/issues/64)) ([bde12a9](https://github.com/Zaimwa9/themis/commit/bde12a960814c615cb82c20c58cf5345dd37e65c)), closes [#61](https://github.com/Zaimwa9/themis/issues/61)

## [0.7.1](https://github.com/Zaimwa9/themis/compare/v0.7.0...v0.7.1) (2026-07-15)


### Bug Fixes

* keep enabled review categories visible ([#58](https://github.com/Zaimwa9/themis/issues/58)) ([7c1eeaf](https://github.com/Zaimwa9/themis/commit/7c1eeaf22ec9ffbf541d508677b377361d1e0ca4))

## [0.7.0](https://github.com/Zaimwa9/themis/compare/v0.6.0...v0.7.0) (2026-07-15)


### Features

* review canaries and trusted-context opt-in for this repo ([#48](https://github.com/Zaimwa9/themis/issues/48)) ([1160b05](https://github.com/Zaimwa9/themis/commit/1160b057d58a3799ca8006d208e0869b2f51adc1))
* skills bridge — synthesized index for engines without native skill discovery ([#54](https://github.com/Zaimwa9/themis/issues/54)) ([9ba5e23](https://github.com/Zaimwa9/themis/commit/9ba5e2330c5c302a8f72b61e48f7d7aceb03a5e0))


### Bug Fixes

* restore canonical review presentation ([#55](https://github.com/Zaimwa9/themis/issues/55)) ([c751bac](https://github.com/Zaimwa9/themis/commit/c751bac2af8f60fd34362e815ac58a639097e0c6))

## [0.6.0](https://github.com/Zaimwa9/themis/compare/v0.5.0...v0.6.0) (2026-07-15)


### Features

* opt-in trusted native context and skills for review agents ([#43](https://github.com/Zaimwa9/themis/issues/43)) ([3c2bf83](https://github.com/Zaimwa9/themis/commit/3c2bf837c7bc80edea9186bc1044c904ce52a541))


### Bug Fixes

* restore full-dress review defaults ([#46](https://github.com/Zaimwa9/themis/issues/46)) ([750bff5](https://github.com/Zaimwa9/themis/commit/750bff59d8b04e0088944f47be689825495aaea1))


### Refactoring

* extract learning orchestration service ([#45](https://github.com/Zaimwa9/themis/issues/45)) ([e76bce9](https://github.com/Zaimwa9/themis/commit/e76bce9c550822dc7e059d287f2f6ce279201220))

## [0.5.0](https://github.com/Zaimwa9/themis/compare/v0.4.0...v0.5.0) (2026-07-15)


### Features

* acknowledged findings stop driving the verdict ([#38](https://github.com/Zaimwa9/themis/issues/38)) ([fbec013](https://github.com/Zaimwa9/themis/commit/fbec013b44d958d2762cd68148d9bc16f056b28e))
* tri-state review modules and packaged default doctrine ([#39](https://github.com/Zaimwa9/themis/issues/39)) ([66e1595](https://github.com/Zaimwa9/themis/commit/66e1595e5b51f22e8cf3aa07a1db51ecbf440e42))


### Documentation

* folded findings keep pointers, context is cap-bounded ([#42](https://github.com/Zaimwa9/themis/issues/42)) ([c899e9d](https://github.com/Zaimwa9/themis/commit/c899e9dfa12bf04ecd738e0c4015792656aef256))

## [0.4.0](https://github.com/Zaimwa9/themis/compare/v0.3.0...v0.4.0) (2026-07-14)


### Features

* instance-level repo-config fallback via THEMIS_DEFAULT_REPO_CONFIG ([#35](https://github.com/Zaimwa9/themis/issues/35)) ([cd4688f](https://github.com/Zaimwa9/themis/commit/cd4688f470111f19b05eec36196c13659257e0e7))
* per-repo learnings memory with human-reviewed digest PRs ([#24](https://github.com/Zaimwa9/themis/issues/24)) ([392f7cc](https://github.com/Zaimwa9/themis/commit/392f7cce1a1b65ce91e7531a8cf26489d55afbe3))


### Bug Fixes

* sync uv.lock on release PRs from the release workflow ([36c0888](https://github.com/Zaimwa9/themis/commit/36c0888e061b42b987c50fb1435e7b41fd509e79)), closes [#15](https://github.com/Zaimwa9/themis/issues/15)
* sync uv.lock with 0.3.0 release version bump ([e9bf2e3](https://github.com/Zaimwa9/themis/commit/e9bf2e34c97f90276fa023eabc6008c8c4b6d3aa))
* validate synced release branch in-job; document required App fields ([2f254cd](https://github.com/Zaimwa9/themis/commit/2f254cd5690605a0096594fb95b332458cece59a))


### Documentation

* add AGENTS.md agent guide, imported by CLAUDE.md ([76c8406](https://github.com/Zaimwa9/themis/commit/76c84065e17c981c32e3f90f05eeeec9ac758b5f))
* organize README around three setup paths ([1da6496](https://github.com/Zaimwa9/themis/commit/1da649634807860ded84b52ff9b505c425c6b7ba))
* seed codex auth via exec pipe, not docker cp ([a2bdbe2](https://github.com/Zaimwa9/themis/commit/a2bdbe274bc0fce4ba0ccb738da168db934660e4))
* setup paths, agent guide, and release lockfile sync ([cd4719b](https://github.com/Zaimwa9/themis/commit/cd4719b5f57cfc3a1bfb92cb4024e53faaba7985))

## [0.3.0](https://github.com/Zaimwa9/themis/compare/v0.2.0...v0.3.0) (2026-07-14)


### Features

* drop qwen engine; Coding Plan ToS forbids unattended use ([#21](https://github.com/Zaimwa9/themis/issues/21)) ([8445c14](https://github.com/Zaimwa9/themis/commit/8445c144e9686fbea340ef2d304c954c4acf0319))
* GLM engine via Claude Code API mode ([e2b15fd](https://github.com/Zaimwa9/themis/commit/e2b15fd2898cbfe6fe3e01f51e037112b65ee57e))
* glm engine via Claude Code API mode ([#20](https://github.com/Zaimwa9/themis/issues/20)) ([ab1f4b8](https://github.com/Zaimwa9/themis/commit/ab1f4b868cf01dcefbf51cd96d7f2bdb3451fca4))
* make reviews adaptive and CI-aware ([d5536cc](https://github.com/Zaimwa9/themis/commit/d5536ccaf1942a69b02f483631990e72314f478d))
* make reviews adaptive and CI-aware ([af1a42a](https://github.com/Zaimwa9/themis/commit/af1a42a341d0a6fed8454e43528021bcce90dbe2))
* qwen engine via Claude Code API mode ([#21](https://github.com/Zaimwa9/themis/issues/21)) ([f395170](https://github.com/Zaimwa9/themis/commit/f395170db89b4168623005aa6921d487ab2568e8))
* register glm and qwen engines; redact provider keys ([#20](https://github.com/Zaimwa9/themis/issues/20), [#21](https://github.com/Zaimwa9/themis/issues/21)) ([e49e7e6](https://github.com/Zaimwa9/themis/commit/e49e7e644bd8743c83a38de4eb6a94f85b93a981))
* wire glm through the bootstrap flow and resolve README merge ([109d98a](https://github.com/Zaimwa9/themis/commit/109d98a220227450c0b1d95c1f82b9dea2ee6c70))


### Bug Fixes

* drop glm text quota markers; ambiguous failures stay retryable ([bb7cb41](https://github.com/Zaimwa9/themis/commit/bb7cb412811867b98ef58683adbc49acf04e9aad))
* window-qualify glm exhaustion marker to avoid generic-output matches ([4705339](https://github.com/Zaimwa9/themis/commit/470533978b1f446392271dda5c4fbca9caf40b0e))


### Documentation

* add CI permissions to manual App setup ([92d1a63](https://github.com/Zaimwa9/themis/commit/92d1a63e65fc0acb710ba363dbbebcdabb327bae))
* design spec for GLM and Qwen engines ([#20](https://github.com/Zaimwa9/themis/issues/20), [#21](https://github.com/Zaimwa9/themis/issues/21)) ([4e49dde](https://github.com/Zaimwa9/themis/commit/4e49dde2333ec86743d1209e10bec1dc34cd2152))
* extend GLM/Qwen coverage across Quickstart deployment sample and Engines notes ([2e68437](https://github.com/Zaimwa9/themis/commit/2e68437fed5c30588d6c57e42b712db68d8e8c49))
* glm and qwen engine setup, config, and security posture ([#20](https://github.com/Zaimwa9/themis/issues/20), [#21](https://github.com/Zaimwa9/themis/issues/21)) ([6ca6766](https://github.com/Zaimwa9/themis/commit/6ca676650d3c15c2809c439da248a55671cea22a))
* guide for contributing a new engine / model provider ([4160042](https://github.com/Zaimwa9/themis/commit/4160042e1ac16da903d164ffd53ccb331867ec6c))
* implementation plan for GLM and Qwen engines ([#20](https://github.com/Zaimwa9/themis/issues/20), [#21](https://github.com/Zaimwa9/themis/issues/21)) ([eea7d06](https://github.com/Zaimwa9/themis/commit/eea7d061329974f6d37dedd22c60e13dbbe7bff0))
* mark implementation plan as historical, superseded scope ([204d10d](https://github.com/Zaimwa9/themis/commit/204d10d87320c270c69726b515bb37d1d333e738))
* mention ngrok token as a permitted pause in agent prompt ([c7f9ecb](https://github.com/Zaimwa9/themis/commit/c7f9ecbf067437788afad93078bb5614ac21f4fa))
* reframe manifest quickstart as testing-only, add agent prompt ([77e0d90](https://github.com/Zaimwa9/themis/commit/77e0d909efb1e5d2649105f6480084030c2182e0))
* reframe manifest quickstart as testing-only, add agent prompt ([cb4cbb6](https://github.com/Zaimwa9/themis/commit/cb4cbb6c6aa745ccd89f49435e417bf63ee2449a))


### Refactoring

* parameterize ClaudeEngine for API-mode subclasses ([#20](https://github.com/Zaimwa9/themis/issues/20), [#21](https://github.com/Zaimwa9/themis/issues/21)) ([ba9c3ba](https://github.com/Zaimwa9/themis/commit/ba9c3baca8ba7bc8ec2e5ef5c05074fadca37907))

## [0.2.0](https://github.com/Zaimwa9/themis/compare/v0.1.2...v0.2.0) (2026-07-13)


### Features

* automate GitHub App bootstrap ([72a635f](https://github.com/Zaimwa9/themis/commit/72a635fbb3b053cef69fc0fa9cc9fb3f32bba6bf))
* automate GitHub App bootstrap ([a7d1b15](https://github.com/Zaimwa9/themis/commit/a7d1b159b8ba638cbf2da323a2eae456d59737d6))


### Bug Fixes

* decode quoted git diff paths ([5fda16e](https://github.com/Zaimwa9/themis/commit/5fda16ec9dd0fe931b1879e2c7928074756a9269))
* surface bot mention after bootstrap ([c384316](https://github.com/Zaimwa9/themis/commit/c38431647e9c8e19e5dc09c81280a77deae9c1d7))
* validate inline review diff anchors ([f6a0aad](https://github.com/Zaimwa9/themis/commit/f6a0aadfca1276f5aed1c9db0c13594b78b03003))

## [0.1.2](https://github.com/Zaimwa9/themis/compare/v0.1.1...v0.1.2) (2026-07-13)


### Bug Fixes

* gate review extra context to trusted comment authors ([e5ef123](https://github.com/Zaimwa9/themis/commit/e5ef123926736c6f613a0f1523d2fbfcd94d0d93))
* gate review extra context to trusted comment authors ([826694a](https://github.com/Zaimwa9/themis/commit/826694af2d58dcbbfe581a18686aaa95a54c0d36))

## [0.1.1](https://github.com/Zaimwa9/themis/compare/v0.1.0...v0.1.1) (2026-07-12)


### Documentation

* surface headless mode in the quickstart ([5d8f0f9](https://github.com/Zaimwa9/themis/commit/5d8f0f9fa4821fff6df263ea3877dbf2a1c184d2))

## 0.1.0 (2026-07-12)


### Features

* allow review command context ([e9b8447](https://github.com/Zaimwa9/themis/commit/e9b8447e1f9bb068008fe6a4d4496781e0d4c221))
* allow review command context ([fbc4d90](https://github.com/Zaimwa9/themis/commit/fbc4d90acf7859c5082b0132fa671b5c6ecdd457))
* app factory with identity resolution and webhook self-registration ([c139353](https://github.com/Zaimwa9/themis/commit/c139353a36382b006c00e69910f48a217df00e74))
* claude engine adapter ([88ac272](https://github.com/Zaimwa9/themis/commit/88ac272bc06c4ad9ca8dc47f690c5861bc76ce8e))
* claude engine with per-repo selection and outbound redaction ([99d75e8](https://github.com/Zaimwa9/themis/commit/99d75e8cc7485df13058a009b62a3eef33bf7218))
* dockerfile, compose with tunnel profile, env example ([efabee5](https://github.com/Zaimwa9/themis/commit/efabee52429a682caab7c3e58e0c995fb7bddbbb))
* doctrine-house-rules-for-engine-symmetry-cli-verification-and-marker-misfires ([8f5fba2](https://github.com/Zaimwa9/themis/commit/8f5fba26e668af33658bbb453c1f1aa6ab3adec0))
* engine and web_access configuration ([f97f678](https://github.com/Zaimwa9/themis/commit/f97f67812ffa7aa2d3b77bf557eeacc25806c61d))
* engine registry and resolve ([7b1c66c](https://github.com/Zaimwa9/themis/commit/7b1c66c6a5ad20f27779387176a46b40d295d3e0))
* engines package with shared runner and codex adapter ([f727dcf](https://github.com/Zaimwa9/themis/commit/f727dcfa160763026cb3721f9b16cd4e7a08d1a4))
* in-memory job queue with dedup, timeout, cancellation ([a652e92](https://github.com/Zaimwa9/themis/commit/a652e9281fb3348519f9d4d5c62a2156ca6c2d8e))
* isolate agent execution from GitHub credentials ([68fd240](https://github.com/Zaimwa9/themis/commit/68fd2402127faa8aac99023f9db8892a7b21b61f))
* isolate agent execution from GitHub credentials ([6a665d2](https://github.com/Zaimwa9/themis/commit/6a665d2cf1d1e920cec1a8a2d32637e0a650bb19))
* outbound secret redaction ([d48357f](https://github.com/Zaimwa9/themis/commit/d48357f3d611bb0a48e4df27d1ef2ca044f85f7f))
* per-repo engine resolution, availability gate, outbound redaction ([f3f6a23](https://github.com/Zaimwa9/themis/commit/f3f6a23d6933b18abd1b273db558161eb1258edc))
* port events parsing, multi-repo, auto flag on review jobs ([617e32e](https://github.com/Zaimwa9/themis/commit/617e32efa3ca939873f614e13e80f5cb57df40d4))
* port github auth and add app-level endpoints (slug, installation, webhook config) ([8815d8f](https://github.com/Zaimwa9/themis/commit/8815d8fc9a294ea140889ad149f2b6e78625c3e6))
* port github client and add default-branch file fetch ([e6b8e17](https://github.com/Zaimwa9/themis/commit/e6b8e17836ced1a1d5c490c889d36f22da4d24ed))
* port prompts with .themis/review.md doctrine path ([e9bb66b](https://github.com/Zaimwa9/themis/commit/e9bb66b133f2c4eae06d58e76e11b02bd558b08d))
* port review service with per-repo config and queue job runners ([36bfee7](https://github.com/Zaimwa9/themis/commit/36bfee754d9f9bd031246db81122f2214ad7debd))
* port security, output, workspace, codex modules with tests ([7610b5e](https://github.com/Zaimwa9/themis/commit/7610b5eef29cd64b682a47e17580922905597556))
* rocket reaction on the trigger comment ([259d264](https://github.com/Zaimwa9/themis/commit/259d2645d26068c76aeca17344bdf4eaf1baa0bc))
* rocket-reaction-on-trigger-comment-instead-of-pr-body ([a4b50e8](https://github.com/Zaimwa9/themis/commit/a4b50e8c70672425efa17d647c400ee8ecdc0c3d))
* settings from env and per-repo config with defaults ([69aad0a](https://github.com/Zaimwa9/themis/commit/69aad0ad554d082e017785e3d2da61ffca9668a5))
* ship claude cli in the image and document engine env ([ca92222](https://github.com/Zaimwa9/themis/commit/ca922224a32352fa0eb01e73564a2dbb93358824))
* startup default-engine availability warning ([f43c3a6](https://github.com/Zaimwa9/themis/commit/f43c3a6dff3ece476e1cae93d8efb9c2c3ebf472))
* stricter review prompt: tool verification, sibling symmetry, assumptions disclosure ([753619b](https://github.com/Zaimwa9/themis/commit/753619b7717b20bbfa2ed7b3ea9e32f1044051ff))
* themis-reviews-itself-doctrine ([d57bc18](https://github.com/Zaimwa9/themis/commit/d57bc187bb74e7a096ab6a16b373258b5131e4d4))
* unverified-suspected-defects-stay-findings-at-full-severity ([09badf2](https://github.com/Zaimwa9/themis/commit/09badf239f13071f906e88d628585fda7eed3edc))
* verification-symmetry-misfire-doctrine-and-assumptions-section-in-review-prompt ([027db11](https://github.com/Zaimwa9/themis/commit/027db111d1370a02db9d19fc2da09e44ed8a2baa))
* webhook adapter and trigger API over one enqueue path ([e13ccd7](https://github.com/Zaimwa9/themis/commit/e13ccd7cd00f0b48ee6eadd2e5d629d235b4ee8d))


### Bug Fixes

* address agent-isolation review follow-ups ([6787506](https://github.com/Zaimwa9/themis/commit/67875061396a153724fe2abfbbf6902677930cb1))
* address isolated agent review findings ([a758406](https://github.com/Zaimwa9/themis/commit/a7584066892c01b57214a30e7e2d167f97a4a312))
* harden engine trust boundaries ([26cfc6d](https://github.com/Zaimwa9/themis/commit/26cfc6da125d4f5d40ccefce9d86e7155111dba5))
* isolate codex from repo configuration ([8b21ef2](https://github.com/Zaimwa9/themis/commit/8b21ef273bc3dd80cd3a199e690ff85ab9748f57))
* portable base64 private key command in .env.example ([03f1643](https://github.com/Zaimwa9/themis/commit/03f1643b88537a6785f0932fbab951f1e16a3eaf))
* queue each manual review trigger ([84097b5](https://github.com/Zaimwa9/themis/commit/84097b5cafc36a953a9e5e2562b857a221d4b90f))
* redact engine error tails at source ([d8af1dc](https://github.com/Zaimwa9/themis/commit/d8af1dc1e8f69f6ed0c071a88b23e41429d68f98))


### Documentation

* complete README quickstart and operator guides ([f7d43ba](https://github.com/Zaimwa9/themis/commit/f7d43ba05a3ee8782239bc2ca533eb917a6b6cc7))
* doctrine-guide-and-image-based-quickstart-with-agent-setup-prompt ([f17030d](https://github.com/Zaimwa9/themis/commit/f17030d545fbf8da13a3778d8496ce048809b3c7))
* engines, web access, and outbound redaction ([a98a124](https://github.com/Zaimwa9/themis/commit/a98a12406bde40e987db249e55e6d441437923d3))
* example .themis starter kit (config and review doctrine template) ([77de12f](https://github.com/Zaimwa9/themis/commit/77de12f449713a047c51a2baaf26a2d25191e90a))
* fix image quickstart review findings ([6a1713b](https://github.com/Zaimwa9/themis/commit/6a1713bd9554be8040a33e04b0104de92589c35b))
* local development section and stale dockerignore entry ([d4d12d8](https://github.com/Zaimwa9/themis/commit/d4d12d8d3bbdb0753b08e1c5cc94d79a0feb58c2))
* replace-agent-setup-prompt-with-one-line-hint ([67b8723](https://github.com/Zaimwa9/themis/commit/67b872334d15d593b1ba2fef0e19fb0ae7b03032))
* require Claude Max for Opus reviews ([101287f](https://github.com/Zaimwa9/themis/commit/101287ff4f5ca028c765a542a8ccde179ee471c6))
* self-doctrine, image-based quickstart, doctrine guide ([6f22d9a](https://github.com/Zaimwa9/themis/commit/6f22d9a679a85e3c6a0344b07ef5329970c1f7a0))


### Refactoring

* review nit batch in tests, queue logging, gitignore ([ed8f435](https://github.com/Zaimwa9/themis/commit/ed8f4352d921a1dad3815da738ea25b514d5036c))
* use app installation as repository scope ([2cdb038](https://github.com/Zaimwa9/themis/commit/2cdb03854eda393ab30358f20919fcc87a931823))
