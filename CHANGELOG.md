# [1.3.0](https://github.com/OpenDisplayEval/display-measure/compare/v1.2.0...v1.3.0) (2026-08-31)


### Features

* **artifact:** keep the spectrum behind each reading, with its provenance ([ccb6537](https://github.com/OpenDisplayEval/display-measure/commit/ccb65375544cf29075529545ec722babe1972674))
* **artifact:** write the seam file as CSMF with a provenance block ([6a7e40f](https://github.com/OpenDisplayEval/display-measure/commit/6a7e40fd95918ba3f03f7eabc1373dae4f4f46c2))
* **hybrid:** reconstruct a colorimeter-routed row's spectrum from its bright reading ([7d9e152](https://github.com/OpenDisplayEval/display-measure/commit/7d9e152c53c39eecea9b449957800cec95193354))

# [1.2.0](https://github.com/OpenDisplayEval/display-measure/compare/v1.1.0...v1.2.0) (2026-08-30)


### Features

* **artifact:** record the wire encoding; schema measurements/2 ([3cec430](https://github.com/OpenDisplayEval/display-measure/commit/3cec43023f307f9751cb44b5c1a86ae45630b4e3))
* **session:** declare the wire encoding per session ([0ee1705](https://github.com/OpenDisplayEval/display-measure/commit/0ee17055569d2296f7c2e8715a64e3f5404a4f9d))

# [1.1.0](https://github.com/OpenDisplayEval/display-measure/compare/v1.0.0...v1.1.0) (2026-08-29)


### Features

* **cli:** cancel a running session with Ctrl-C ([73f4ede](https://github.com/OpenDisplayEval/display-measure/commit/73f4ede7ea1b013406f8d69adfb6e03d3372d009))
* **session:** cancel a session between patch steps ([d5c9f33](https://github.com/OpenDisplayEval/display-measure/commit/d5c9f338dd4bb9d8d2074065f7ab5c186ecba2ad))
* **session:** name the derivation-fitness gate on the event stream ([b3408a0](https://github.com/OpenDisplayEval/display-measure/commit/b3408a06a0b146fbb8c4d481dc3a985400b5094e))
* **session:** refuse an artifact whose own rows contradict each other ([324a91d](https://github.com/OpenDisplayEval/display-measure/commit/324a91da2a5fc460403b8bc0b0939c8bf9b48150))
* **session:** report the lifecycle as a structured event stream ([a231919](https://github.com/OpenDisplayEval/display-measure/commit/a231919197a6659efe7776b6faf1bdc841797afa))

# 1.0.0 (2026-08-29)


### Bug Fixes

* **artifact:** refuse strings the renderer cannot represent ([9fe387b](https://github.com/OpenDisplayEval/display-measure/commit/9fe387bbb11b359b1e95d1036d2877c61926cbe3))


### Features

* **session:** port the session core from color-wrangler ([e8f3c55](https://github.com/OpenDisplayEval/display-measure/commit/e8f3c55056b2d5899c740f158213c03ce89181b0))
