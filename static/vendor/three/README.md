# static/vendor/three — three.js 内嵌资产

- 来源：npm registry 官方 tarball `https://registry.npmjs.org/three/-/three-0.187.0.tgz`
- 版本：**0.187.0**（r187）
- Tarball sha512（npm registry `dist.integrity`）：
  `sha512-Qiu7R+Cmh8CstZsY1uJOSWnVlfo7ZtNLV7mF8Jltk3LB3AtDu9cRZlcnRKkAk8Y4z6KKw35z7Y7Urd7jWY8etA==`
- 逐文件 sha512（base64，复制后复算留档）：
  - `three.module.js` 658,900 B
    `sha512-YkjJhgByi8ySOtzBy85MfNK2xEuCI1cmtu2KrMRH8+qPU3SqF86u/x+ULkCebJSNK2sL6zkYGKWAqkNc7RdhZw==`
  - `three.core.js` 1,474,799 B
    `sha512-TeU5i/TRAQrBlAeoc8J+RjivF8BFwtV+G5B0Eh/GcMpIIQ/HikCVbutjYnXFDjai3AGaB7cLJ66ELWXciDSnxw==`
  - `LICENSE.txt` 1,081 B（上游 MIT 全文）
- 许可：MIT（上游版权：three.js authors，2010-2026）
- 用途：`static/js/gamut-three.js` 经 `import("/static/vendor/three/three.module.js")`
  动态加载，注册 gamut-view 渲染器注册表的 `'three'` 渲染器（V0.4 真 3D）。

更新方法：改版本号后重复「下载 → registry 元数据 sha512 校验 → 复制 → 复算留档」流程，
并同步更新本文件与 `docs/V0.4-实施规划.md`。
