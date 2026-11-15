# 第三方测试资产许可说明

本目录的 ICC 特性文件取自 LittleCMS 官方仓库 testbed（https://github.com/mm2/Little-CMS，
master 分支 testbed/ 目录），用于跨平台自动化测试。

LittleCMS 以 MIT 许可证发布：

```
Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice shall be included in all copies
or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE
OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

| 文件 | 用途 | 来源 |
|---|---|---|
| testbed_cmyk.icc | CMYK 输出工艺 fixture（prtr/CMYK/Lab） | littlecms testbed/test2.icc |
| ibm-t61.icc | RGB 显示工艺 fixture（mntr/RGB/XYZ） | littlecms testbed/ibm-t61.icc |

GRAY profile 不入库（避免构造文件入库争议），由 `tests/conftest.py` 在运行时
按 ICC 规范手工构造最小合法灰度 profile（仅含 desc/wtpt/kTRC 标签）。
