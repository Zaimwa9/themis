# Changelog

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
